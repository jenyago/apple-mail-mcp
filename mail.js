const started = Date.now();
const mail = Application('com.apple.mail');
const meta = {};
// p.allowed_emails is null (every account) or a lowercase address allowlist. Every
// account access below goes through permitted(), so this is the single enforcement point.
function permitted(a) {
    return !p.allowed_emails || a.emailAddresses().some(e => p.allowed_emails.includes(String(e).toLowerCase()));
}
function addressOf(s) {
    const m = /<([^<>]+)>\s*$/.exec(s);
    return (m ? m[1] : s).trim().toLowerCase();
}
function account(id) {
    const matches = mail.accounts().filter(a => a.id() === id);
    if (matches.length !== 1) throw Error('Account not found; use list_accounts.');
    if (!permitted(matches[0])) throw Error('Account not permitted by APPLE_MAIL_ACCOUNTS.');
    meta.account = matches[0].emailAddresses()[0] || null;
    return matches[0];
}
function mailbox() {
    let parent = account(p.account_id);
    if (p.inbox) {
        // The inbox is named 'INBOX' on most providers but 'Inbox' on some: match the top level case-insensitively.
        const inboxes = parent.mailboxes().filter(b => b.name().toLowerCase() === 'inbox');
        if (inboxes.length !== 1) throw Error('Inbox not found or ambiguous; use list_mailboxes.');
        return inboxes[0];
    }
    for (const name of p.mailbox_path) {
        const found = parent.mailboxes().filter(b => b.name() === name);
        if (found.length !== 1) throw Error('Mailbox path missing or ambiguous; use list_mailboxes.');
        parent = found[0];
    }
    return parent;
}
function summary(m) {
    const subject = String(m.subject() || ''), sender = String(m.sender() || '');
    return {id: m.id(), subject: subject.slice(0, 1000), sender: sender.slice(0, 1000),
        ...(subject.length > 1000 ? {subject_truncated:true} : {}),
        ...(sender.length > 1000 ? {sender_truncated:true} : {}),
        date_received: m.dateReceived().toISOString(), read: m.readStatus()};
}
function execute() {
    if (p.op === 'accounts') return mail.accounts().filter(permitted).map(a => ({id:a.id(), name:a.name(), email_addresses:a.emailAddresses()}));
    if (p.op === 'mailboxes') {
        const out = [];
        function walk(parent, path) {
            if (path.length > 30) throw Error('Mailbox nesting exceeds 30 levels.');
            for (const b of parent.mailboxes()) {
                const next = path.concat([b.name()]);
                out.push({path:next, unread_count:b.unreadCount()});
                walk(b, next);
            }
        }
        walk(account(p.account_id), []);
        return out;
    }
    if (p.op === 'search') {
        // One bulk id() call, then access by id. Indexing box.messages[i] costs time proportional
        // to the mailbox size on every access (~190 ms/property on a 5,000-message Gmail inbox,
        // so a 200-message scan exceeded the timeout); byId is ~8 ms/property.
        const box = mailbox(), ids = box.messages.id(), total = ids.length;
        const end = Math.min(total, p.offset + p.scan_limit), results = [];
        const query = p.query.toLowerCase();
        const recipient = p.recipient ? p.recipient.toLowerCase() : '';
        let i = p.offset, tried = 0, unreadable = 0, lastError = '';
        for (; i < end && results.length < p.limit; i++) {
            // Stop before the caller's timeout kills osascript: Mail keeps working on abandoned
            // requests, which makes the next calls time out too. next_offset resumes the scan.
            // Always scan at least one message so next_offset advances.
            if (i > p.offset && Date.now() - started > p.time_budget_ms) break;
            let entry = null;
            tried++;
            try {
                const m = box.messages.byId(ids[i]);
                if (p.unread_only && m.readStatus()) continue;
                if (query && !(m.subject() + '\n' + m.sender()).toLowerCase().includes(query)) continue;
                const received = m.dateReceived().toISOString();
                if (p.since && received < p.since) continue;
                if (p.until && received > p.until) continue;
                if (recipient && !m.toRecipients().concat(m.ccRecipients(), m.bccRecipients())
                    .some(r => String(r.address()).toLowerCase().includes(recipient))) continue;
                entry = summary(m);
            } catch (e) {
                unreadable++;  // usually a message deleted since the id snapshot
                lastError = String(e);
                continue;
            }
            results.push(entry);
        }
        if (tried > 0 && unreadable === tried) throw Error('No message in the scan window could be read: ' + lastError);
        return {messages:results, total_in_mailbox:total, next_offset:i < total ? i : null,
            partial:unreadable > 0, unreadable_count:unreadable,
            partial_reason:unreadable > 0 ? 'One or more messages could not be read; continue with next_offset where available.' : null,
            search_scope:'Subject and sender substring; optional recipient-address substring and inclusive received-date bounds; in Mail mailbox order. Continue with next_offset.',
            applied_filters:{query_applied:Boolean(p.query), unread_only:p.unread_only,
                since:p.since, until:p.until, recipient_filter_applied:Boolean(p.recipient)},
            ...(p.inbox ? {mailbox_path:[box.name()]} : {})};
    }
    if (p.op === 'read') {
        const matches = mailbox().messages.whose({id:p.message_id})();
        if (matches.length !== 1) throw Error('Message not found in the specified mailbox.');
        const m = matches[0], result = summary(m), body = m.content();
        result.content = body.slice(0, p.max_chars);
        result.content_truncated = body.length > p.max_chars;
        const recipients = m.toRecipients(), recipientCount = recipients.length;
        result.to = recipients.slice(0, 100).map(r => ({
            name:String(r.name() || '').slice(0, 500),
            address:String(r.address() || '').slice(0, 320)}));
        result.to_total = recipientCount;
        result.to_truncated = recipientCount > 100;
        return result;
    }
    if (p.op === 'thread') {
        const box = mailbox(), ids = box.messages.id(), total = ids.length;
        const anchors = box.messages.whose({id:p.message_id})();
        if (anchors.length !== 1) throw Error('Message not found in the specified mailbox.');
        const anchor = anchors[0];
        const headerMessageIds = text => {
            const matches = String(text || '').match(/<[^<>\s]+>/g) || [];
            return matches.map(x => x.slice(1, -1));
        };
        const headerValues = (headers, name) => {
            const unfolded = String(headers || '').replace(/\r?\n[ \t]+/g, ' ');
            const expression = new RegExp('^' + name + ':\\s*(.*)$', 'gim');
            const values = [];
            let match;
            while ((match = expression.exec(unfolded)) !== null) values.push(match[1]);
            return values.join(' ');
        };
        function threadRecord(m, order) {
            const headers = m.allHeaders();
            const directIds = headerMessageIds(m.messageId());
            const ownIds = directIds.length ? directIds : headerMessageIds(headerValues(headers, 'Message-ID'));
            const references = headerMessageIds(headerValues(headers, 'References') + ' ' +
                headerValues(headers, 'In-Reply-To'));
            return {message:m, order, own:ownIds[0] || '', references:Array.from(new Set(references))};
        }
        const anchorRecord = threadRecord(anchor, ids.indexOf(p.message_id));
        if (!anchorRecord.own) throw Error('Thread membership is unavailable: anchor has no RFC Message-ID header.');
        const records = [anchorRecord], seen = new Set([p.message_id]);
        const scanStart = Math.min(total, p.scan_offset), end = Math.min(total, scanStart + p.scan_limit);
        const startedScan = Date.now();
        let i = scanStart, scanned = 0, unreadable = 0, noMessageId = 0, timedOut = false;
        while (i < end) {
            if (Date.now() - startedScan > p.time_budget_ms) {
                timedOut = true;
                break;
            }
            let m;
            try {
                m = box.messages.byId(ids[i]);
                const localId = m.id();
                if (seen.has(localId)) {
                    scanned++;
                } else {
                    const record = threadRecord(m, i);
                    if (!record.own) noMessageId++;
                    records.push(record);
                    seen.add(localId);
                    scanned++;
                }
            } catch (e) {
                unreadable++;
            }
            i++;
        }
        const links = new Map();
        function connect(a, b) {
            if (!links.has(a)) links.set(a, new Set());
            links.get(a).add(b);
        }
        for (const record of records) {
            if (!record.own) continue;
            for (const reference of record.references) {
                connect(record.own, reference);
                connect(reference, record.own);
            }
        }
        const related = new Set([anchorRecord.own]);
        const pending = [anchorRecord.own];
        while (pending.length) {
            const current = pending.pop();
            for (const linked of links.get(current) || []) {
                if (!related.has(linked)) {
                    related.add(linked);
                    pending.push(linked);
                }
            }
        }
        const members = records.filter(record => record.own && related.has(record.own))
            .sort((a, b) => a.order - b.order)
            .map(record => record.message);
        if (!members.some(m => m.id() === p.message_id)) members.unshift(anchor);
        let bodyCharsLeft = p.total_body_chars, bodyBudgetExhausted = false;
        const page = members.slice(p.offset, p.offset + p.limit).map(m => {
            const item = summary(m);
            if (p.include_bodies) {
                if (bodyCharsLeft <= 0) {
                    item.content = '';
                    item.content_truncated = true;
                    bodyBudgetExhausted = true;
                } else {
                    const body = m.content(), take = Math.min(p.max_chars, bodyCharsLeft);
                    item.content = body.slice(0, take);
                    item.content_truncated = body.length > take;
                    bodyCharsLeft -= item.content.length;
                }
            }
            return item;
        });
        const scanWindowComplete = !timedOut && i === end;
        const nextScanOffset = i < total ? i : null;
        const scanComplete = scanStart === 0 && scanWindowComplete && end === total && unreadable === 0;
        const reasons = [];
        if (timedOut) reasons.push('time_budget_exhausted');
        if (scanStart > 0) reasons.push('continued_scan_window');
        if (nextScanOffset !== null && !timedOut) reasons.push('scan_limit_reached');
        if (unreadable) reasons.push('message_headers_unreadable');
        if (noMessageId) reasons.push('messages_without_message_id');
        return {messages:page, discovered_count:members.length,
            next_offset:p.offset + page.length < members.length ? p.offset + page.length : null,
            scan_complete:scanComplete, scan_window_complete:scanWindowComplete,
            mailbox_scan_finished:nextScanOffset === null,
            complete_under_rfc_headers:scanComplete && noMessageId === 0,
            partial_reasons:reasons, continuation_scan_offset:nextScanOffset,
            continuation_instruction:nextScanOffset !== null
                ? 'Continue with this scan_offset and the same anchor/account/mailbox; append summaries from each window. Each window links its RFC headers to the anchor.'
                : scanStart > 0 ? 'This is the final scan window; combine its summaries with the earlier windows. Completeness across windows is not retained by the server.'
                    : !(scanComplete && noMessageId === 0) ? 'The scan ended with unreadable or missing headers; RFC-linked membership may be incomplete.'
                        : null,
            body_budget_exhausted:bodyBudgetExhausted,
            scanned, total_in_mailbox:total,
            native_thread_membership_supported:false,
            membership_method:'RFC Message-ID, References, and In-Reply-To links within the specified mailbox. Mail exposes no native conversation-membership property through JXA.',
            ...(unreadable ? {scan_error:'One or more messages could not be read; details omitted.'} : {})};
    }
    if (p.op === 'statistics') {
        const box = mailbox(), ids = box.messages.id(), total = ids.length;
        const since = p.since || null, until = p.until || null;
        const hasWindow = since !== null || until !== null;
        let dateCount = hasWindow ? 0 : null, scanned = 0, unreadable = 0, timedOut = false;
        if (hasWindow) {
            const end = Math.min(total, p.scan_limit), start = Date.now();
            for (let i = 0; i < end; i++) {
                if (Date.now() - start > p.time_budget_ms) {
                    timedOut = true;
                    break;
                }
                try {
                    const received = box.messages.byId(ids[i]).dateReceived().toISOString();
                    if ((!since || received >= since) && (!until || received <= until)) dateCount++;
                    scanned++;
                } catch (e) {
                    unreadable++;
                }
            }
        }
        const dateWindowComplete = hasWindow && !timedOut && total <= p.scan_limit && unreadable === 0;
        let unreadCount = null, unreadCountUnavailableReason = null;
        try {
            unreadCount = box.unreadCount();
        } catch (e) {
            unreadCountUnavailableReason = 'Mail did not provide the mailbox unread count.';
        }
        return {account_id:p.account_id, mailbox_path:p.mailbox_path,
            total_count:total, unread_count:unreadCount,
            unread_count_unavailable_reason:unreadCountUnavailableReason,
            date_window:{since, until, count:dateWindowComplete ? dateCount : null,
                complete:hasWindow ? dateWindowComplete : null, scanned,
                total_in_mailbox:total,
                unavailable_reason:!hasWindow ? 'No date bounds supplied.'
                    : dateWindowComplete ? null
                    : timedOut ? 'Statistics time budget exhausted.'
                    : unreadable ? 'Some message dates could not be read.'
                    : 'Mailbox exceeds the bounded date-count scan limit.'},
            statistics_scope:'One explicitly selected account and mailbox; total and unread counts are Mail mailbox counts. No cross-account scan.'};
    }
    if (p.op === 'attachments') {
        const matches = mailbox().messages.whose({id:p.message_id})();
        if (matches.length !== 1) throw Error('Message not found in the specified mailbox.');
        let all, total;
        try {
            all = matches[0].mailAttachments();
            total = all.length;
        } catch (e) {
            throw Error('Mail could not list attachments for this message: ' + String(e));
        }
        const end = Math.min(total, p.offset + p.limit), attachments = [];
        let unreadable = 0, mimeTypeUnavailable = 0;
        for (let i = p.offset; i < end; i++) {
            try {
                const attachment = all[i];
                const name = String(attachment.name() || '');
                const id = String(attachment.id() || '');
                // Mail's JXA bridge reliably throws AppleEvent handler failed for mimeType() on
                // some real attachments (observed on PDFs); the other properties are unaffected,
                // so mimeType degrades independently instead of losing the whole attachment.
                let mimeType = '', mimeTypeAvailable = true;
                try {
                    mimeType = String(attachment.mimeType() || '');
                } catch (e) {
                    mimeTypeAvailable = false;
                    mimeTypeUnavailable++;
                }
                attachments.push({id:id.slice(0, 512), name:name.slice(0, 1024),
                    mime_type:mimeTypeAvailable ? mimeType.slice(0, 255) : null,
                    size_bytes:attachment.fileSize(), downloaded:attachment.downloaded(),
                    ...(id.length > 512 ? {id_truncated:true} : {}),
                    ...(name.length > 1024 ? {name_truncated:true} : {}),
                    ...(mimeTypeAvailable && mimeType.length > 255 ? {mime_type_truncated:true} : {})});
            } catch (e) {
                unreadable++;  // Mail can throw on a whole attachment's core properties (e.g. name, id)
            }
        }
        return {attachments, total, next_offset:end < total ? end : null,
            partial:unreadable > 0, unreadable_count:unreadable,
            partial_reason:unreadable > 0 ? 'One or more attachments could not be read; their metadata is omitted.'
                : mimeTypeUnavailable > 0 ? 'mime_type was unavailable for one or more attachments.' : null,
            attachment_scope:'Metadata only. Files are not retrieved, written, opened, or executed.'};
    }
    if (p.op === 'draft') {
        // Mail files a draft under the sender's account, or its default account when
        // sender is empty, so an allowlist requires an explicit allowed sender.
        if (p.allowed_emails && !p.allowed_emails.includes(addressOf(p.sender || '')))
            throw Error('Sender must be an address listed in APPLE_MAIL_ACCOUNTS.');
        const props = {subject:p.subject, content:p.body, visible:true};
        if (p.sender) props.sender = p.sender;
        const draft = mail.OutgoingMessage(props);
        mail.outgoingMessages.push(draft);
        for (const address of p.to) draft.toRecipients.push(mail.ToRecipient({address:address}));
        mail.save(draft);
        return {id:draft.id(), status:'draft', sent:false};
    }
    throw Error('Unknown operation');
}
JSON.stringify({result: execute(), meta: meta});
