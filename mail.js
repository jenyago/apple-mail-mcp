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
    return {id: m.id(), subject: m.subject(), sender: m.sender(),
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
            search_scope:'Subject and sender, in Mail mailbox order; continue with next_offset.',
            ...(p.inbox ? {mailbox_path:[box.name()]} : {})};
    }
    if (p.op === 'read') {
        const matches = mailbox().messages.whose({id:p.message_id})();
        if (matches.length !== 1) throw Error('Message not found in the specified mailbox.');
        const m = matches[0], result = summary(m), body = m.content();
        result.content = body.slice(0, p.max_chars);
        result.content_truncated = body.length > p.max_chars;
        result.to = m.toRecipients().map(r => ({name:r.name(), address:r.address()}));
        return result;
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
