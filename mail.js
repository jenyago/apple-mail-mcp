const mail = Application('com.apple.mail');
function account(id) {
    const matches = mail.accounts().filter(a => a.id() === id);
    if (matches.length !== 1) throw Error('Account not found; use list_accounts.');
    return matches[0];
}
function mailbox() {
    let parent = account(p.account_id);
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
    if (p.op === 'accounts') return mail.accounts().map(a => ({id:a.id(), name:a.name(), email_addresses:a.emailAddresses()}));
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
        const box = mailbox(), total = box.messages.length;
        const end = Math.min(total, p.offset + p.scan_limit), results = [];
        const query = p.query.toLowerCase();
        let i = p.offset;
        for (; i < end && results.length < p.limit; i++) {
            const m = box.messages[i];
            if (p.unread_only && m.readStatus()) continue;
            if (query && !(m.subject() + '\n' + m.sender()).toLowerCase().includes(query)) continue;
            results.push(summary(m));
        }
        return {messages:results, total_in_mailbox:total, next_offset:i < total ? i : null,
            search_scope:'Subject and sender, in Mail mailbox order; continue with next_offset.'};
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
JSON.stringify(execute());
