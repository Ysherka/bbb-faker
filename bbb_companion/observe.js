() => {
  const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const text = el => (el?.textContent || '').trim();
  const listen = document.querySelector('[data-test="leaveListenOnly"]');
  const endedSelectors = '[data-test="meetingEndedModalTitle"], [data-test="meetingEndedModal"], [data-test="meetingEnded"], [data-test="meetingEndedMessage"]';
  const ended = [...document.querySelectorAll(endedSelectors)].some(visible);
  const waitingHeading = [...document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')].find(el => visible(el)
    && !el.closest('[data-test="chatMessages"]')
    && /(?:(?:встреча|вебинар|конференция|лекция)\s+(?:ещ[её]\s+)?не\s+начал|(?:meeting|session|conference)\s+(?:has\s+)?not\s+(?:yet\s+)?started)/i.test(text(el)));
  const portalText = document.body?.innerText || '';
  const portalWaiting = !document.querySelector('[data-test="chatMessages"], [data-test="audioModal"]')
    && /(?:встреча|вебинар)\s+(?:ещ[её]\s+)?не\s+начал/i.test(portalText)
    && /автоматически\s+присоединитесь|automatically\s+join/i.test(portalText);
  const waiting = !ended && (!!waitingHeading || portalWaiting);
  const disconnected = [...document.querySelectorAll('[data-test="reconnectModal"], [data-test="reconnecting"], [data-test="connectionError"]')].some(visible);
  const own = document.querySelector('[data-test="userListItemCurrent"]');
  const ownId = own?.getAttribute('data-user-id') || own?.getAttribute('data-id') || own?.id || null;
  let count = null;
  for (const selector of ['[data-test="participantCount"]', '[data-test="userListCount"]', '[data-test="userListTitle"]', '[data-test="userListLabel"]']) {
    const el = document.querySelector(selector);
    if (!el) continue;
    const value = el.getAttribute('data-count') || text(el).match(/\d+/)?.[0];
    if (value != null && /^\d+$/.test(value)) { count = Number(value); break; }
  }
  if (count === null) {
    const pattern = /(?:участник[а-яё]*|пользовател[а-яё]*|users|participants)\s*\(([\d\s,]+)\)/i;
    const sidebar = document.querySelector('[data-test="userListContent"]');
    const labels = [...(sidebar?.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]') || []),
      ...document.querySelectorAll('#scroll-box h2, [data-test="userListContainer"] h2')];
    for (const label of labels) {
      const match = text(label).match(pattern);
      if (match) { count = Number(match[1].replace(/[\s,]/g, '')); break; }
    }
    if (count === null && sidebar) {
      const match = (sidebar.innerText || '').match(/(?:^|\n)\s*(?:участник[а-яё]*|пользовател[а-яё]*|users|participants)\s*\((\d+)\)/im);
      if (match) count = Number(match[1]);
    }
  }
  if (count === null) {
    const button = document.querySelector('[data-test="toggleUserList"], button[accesskey="U"]');
    const value = button?.getAttribute('aria-label') || button?.getAttribute('title') || text(button);
    const match = value?.match(/(?:\(|:|\s)(\d+)\)?\s*$/);
    if (match) count = Number(match[1]);
  }
  const scope = document.querySelector('[data-test="publicChat"] [data-test="chatMessages"]')
    || document.querySelector('[data-test="chatMessages"]');
  const rows = [...(scope || document).querySelectorAll('[data-test="chatMessageItem"]')];
  const legacy = [...(scope || document).querySelectorAll('p[data-test="chatUserMessageText"]')]
    .filter(el => !el.closest('[data-test="chatMessageItem"]'));
  let previousName = '';
  const messages = rows.map(row => {
    const content = row.querySelector('[data-test="messageContent"], [data-test="chatUserMessageText"]');
    if (!content) return null;
    const container = row.closest('.chat-message-container');
    const group = row.closest('[data-test="chatMessageGroup"]') || container || row.parentElement;
    const authorEl = row.querySelector('[data-test="chatMessageName"], [data-test="messageAuthor"]') || group?.querySelector('[data-test="chatMessageName"], [data-test="messageAuthor"]');
    const header = container?.querySelector('[role="listitem"]:not([data-test="chatMessageItem"])');
    const name = row.getAttribute('data-author-name') || text(authorEl) || text(header?.firstElementChild?.firstElementChild) || (container ? previousName : '');
    if (name) previousName = name;
    const author = row.getAttribute('data-author-id') || row.getAttribute('data-user-id') || group?.getAttribute('data-user-id') || (name ? 'name:' + name : '');
    const stamp = row.getAttribute('data-timestamp') || row.querySelector('time')?.getAttribute('datetime') || text(row.querySelector('[data-test="chatMessageTime"]'));
    const id = row.getAttribute('data-chat-message-id') || row.getAttribute('data-message-id') || row.id || container?.getAttribute('data-sequence') || (author && stamp ? author + '|' + stamp + '|' + text(content) : null);
    return id && author && name ? {id, author, name, text: text(content)} : null;
  }).filter(Boolean);
  // BBB 2 groups paragraphs under a single author/time metadata block.
  for (const paragraph of legacy) {
    let group = paragraph.parentElement;
    let stampElement = null;
    for (let depth = 0; group && group !== scope && depth < 6; depth++, group = group.parentElement) {
      stampElement = group.querySelector('time');
      if (stampElement) break;
    }
    if (!stampElement || !group || group === scope) continue;
    const metadata = stampElement.parentElement;
    const nameElement = metadata?.firstElementChild;
    const name = text(nameElement?.querySelector('span') || nameElement);
    const stamp = stampElement.getAttribute('datetime') || text(stampElement);
    const siblings = [...group.querySelectorAll('p[data-test="chatUserMessageText"]')];
    const index = siblings.indexOf(paragraph);
    if (!name || !stamp || index < 0) continue;
    const author = group.getAttribute('data-user-id') || 'name:' + name;
    const id = paragraph.getAttribute('data-message-id') || paragraph.id || JSON.stringify(['bbb2', author, stamp, index, text(paragraph)]);
    messages.push({id, author, name, text: text(paragraph)});
  }
  const warnings = [];
  const totalRows = rows.length + legacy.length;
  if (!ended && !waiting) {
    if (count === null) warnings.push('Не найден надёжный счётчик участников; выход по численности приостановлен');
    if (totalRows > messages.length) warnings.push(`Не распознаны сообщения чата: ${totalRows - messages.length} из ${totalRows}`);
    if (!document.querySelector('#message-input')) warnings.push('Поле общего чата не найдено');
  }
  return {listen_only: visible(listen), healthy: visible(listen) && !disconnected && !ended && navigator.onLine,
    total: count, own_id: ownId, ended, waiting, waiting_reason: waiting ? text(waitingHeading) || 'Встреча ещё не началась' : null,
    messages, chat_count: totalRows, chat_parsed: messages.length,
    chat_layout: rows.length ? 'bbb3' : legacy.length ? 'bbb2' : scope ? 'empty' : 'unavailable',
    warning: warnings.join('; ') || null};
}
