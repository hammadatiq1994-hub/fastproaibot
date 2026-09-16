<?php
/**
 * WordPress AI Chatbot Widget (self-contained snippet)
 *
 * Install: paste this entire file into a Code Snippets plugin (run everywhere)
 * or add it to your child theme's functions.php.
 *
 * CONFIG -- edit these constants before going live.
 */
if (!defined('ABSPATH')) {
    exit;
}

if (!defined('AICB_API_URL')) {
    // Local Windows backend. Change to https://api.yourdomain.com when you migrate.
    define('AICB_API_URL', 'http://localhost:5000');
}
if (!defined('AICB_API_KEY')) {
    // Must match API_SECRET_KEY in the Python .env. Never expose this to JavaScript.
    define('AICB_API_KEY', 'change-me-to-a-long-random-secret');
}
if (!defined('AICB_BOT_NAME')) {
    define('AICB_BOT_NAME', 'Alex');
}
if (!defined('AICB_PRIMARY_COLOR')) {
    define('AICB_PRIMARY_COLOR', '#0f766e');
}

add_action('wp_enqueue_scripts', 'aicb_enqueue_assets');
add_action('wp_footer', 'aicb_render_widget');
add_action('wp_ajax_aicb_proxy', 'aicb_proxy');
add_action('wp_ajax_nopriv_aicb_proxy', 'aicb_proxy');

function aicb_enqueue_assets() {
    wp_register_style('aicb-widget', false, array(), '1.0.0');
    wp_enqueue_style('aicb-widget');
    wp_add_inline_style('aicb-widget', aicb_css());

    wp_register_script('aicb-widget', '', array(), '1.0.0', true);
    wp_enqueue_script('aicb-widget');
    wp_add_inline_script('aicb-widget', aicb_js());
}

/**
 * Same-origin proxy so the API secret never ships to the browser.
 */
function aicb_proxy() {
    check_ajax_referer('aicb_nonce', 'nonce');

    $endpoint = isset($_POST['endpoint']) ? sanitize_text_field(wp_unslash($_POST['endpoint'])) : '';
    $allowed  = array(
        'chat'         => '/api/chat',
        'availability' => '/api/availability',
        'book'         => '/api/book',
        'reset'        => '/api/session/reset',
    );
    if (!isset($allowed[$endpoint])) {
        wp_send_json_error(array('error' => 'Invalid endpoint.'), 400);
    }

    $body_raw = isset($_POST['payload']) ? wp_unslash($_POST['payload']) : '{}';
    $decoded  = json_decode($body_raw, true);
    if (!is_array($decoded)) {
        $decoded = array();
    }

    $method = ($endpoint === 'availability') ? 'GET' : 'POST';
    $url    = rtrim(AICB_API_URL, '/') . $allowed[$endpoint];

    $args = array(
        'timeout' => 45,
        'headers' => array(
            'Content-Type' => 'application/json',
            'X-API-Key'    => AICB_API_KEY,
        ),
        'method'  => $method,
    );
    if ($method === 'POST') {
        $args['body'] = wp_json_encode($decoded);
    }

    $response = wp_remote_request($url, $args);
    if (is_wp_error($response)) {
        wp_send_json_error(array('error' => 'Could not reach the assistant. Please try again.'), 502);
    }

    $code = wp_remote_retrieve_response_code($response);
    $body = json_decode(wp_remote_retrieve_body($response), true);
    if (!is_array($body)) {
        $body = array('error' => 'Unexpected response from assistant.');
    }
    status_header($code);
    wp_send_json($body);
}

function aicb_render_widget() {
    $ajax  = esc_url(admin_url('admin-ajax.php'));
    $nonce = wp_create_nonce('aicb_nonce');
    $bot   = esc_html(AICB_BOT_NAME);
    ?>
    <div id="aicb-root" data-ajax="<?php echo $ajax; ?>" data-nonce="<?php echo esc_attr($nonce); ?>" hidden>
        <button type="button" id="aicb-launcher" aria-label="Open chat" aria-expanded="false">
            <svg id="aicb-icon-chat" width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8A2.5 2.5 0 0 1 17.5 16H9l-4 4v-4.5A2.5 2.5 0 0 1 4 13.5v-8Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
            </svg>
            <svg id="aicb-icon-close" width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true" hidden>
                <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
        </button>
        <section id="aicb-panel" role="dialog" aria-labelledby="aicb-title" hidden>
            <header id="aicb-header">
                <div>
                    <strong id="aicb-title"><?php echo $bot; ?></strong>
                    <span id="aicb-status">Online</span>
                </div>
                <button type="button" id="aicb-close" aria-label="Close chat">&times;</button>
            </header>
            <div id="aicb-messages" role="log" aria-live="polite"></div>
            <div id="aicb-quick"></div>
            <form id="aicb-book" hidden>
                <p id="aicb-book-slot"></p>
                <input id="aicb-name" type="text" placeholder="Full name *" required maxlength="80">
                <input id="aicb-email" type="email" placeholder="Email *" required maxlength="120">
                <input id="aicb-phone" type="tel" placeholder="Phone (optional)" maxlength="32">
                <input id="aicb-reason" type="text" placeholder="Reason / service" maxlength="200">
                <div class="aicb-book-actions">
                    <button type="submit">Confirm booking</button>
                    <button type="button" id="aicb-book-cancel">Cancel</button>
                </div>
            </form>
            <form id="aicb-form" autocomplete="off">
                <input id="aicb-input" type="text" maxlength="2000" placeholder="Type a message..." aria-label="Message">
                <button type="submit" id="aicb-send">Send</button>
            </form>
        </section>
    </div>
    <?php
}

function aicb_css() {
    $color = AICB_PRIMARY_COLOR;
    return <<<CSS
:root { --aicb-primary: {$color}; }
#aicb-root { position: fixed; right: 20px; bottom: 20px; z-index: 999999; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; }
#aicb-launcher { width: 60px; height: 60px; border-radius: 50%; border: 0; background: var(--aicb-primary); color: #fff; cursor: pointer; box-shadow: 0 10px 24px rgba(15,118,110,.35); display: flex; align-items: center; justify-content: center; transition: transform .2s ease, box-shadow .2s ease; }
#aicb-launcher:hover { transform: translateY(-2px); }
#aicb-panel { position: absolute; right: 0; bottom: 76px; width: min(380px, calc(100vw - 24px)); height: min(560px, calc(100vh - 120px)); background: #fff; border-radius: 16px; box-shadow: 0 18px 50px rgba(15,23,42,.22); display: flex; flex-direction: column; overflow: hidden; opacity: 0; transform: translateY(12px) scale(.98); pointer-events: none; transition: opacity .22s ease, transform .22s ease; }
#aicb-root.is-open #aicb-panel { opacity: 1; transform: none; pointer-events: auto; }
#aicb-header { background: var(--aicb-primary); color: #fff; padding: 14px 16px; display: flex; align-items: center; justify-content: space-between; }
#aicb-status { display: block; font-size: 12px; opacity: .85; }
#aicb-close { background: transparent; border: 0; color: #fff; font-size: 26px; line-height: 1; cursor: pointer; }
#aicb-messages { flex: 1 1 0; min-height: 0; overflow-y: auto; padding: 16px; background: #f8fafc; display: flex; flex-direction: column; gap: 10px; }
.aicb-msg { max-width: 82%; padding: 10px 12px; border-radius: 14px; font-size: 14px; line-height: 1.45; white-space: pre-wrap; word-wrap: break-word; }
.aicb-msg.bot { align-self: flex-start; background: #fff; color: #0f172a; border: 1px solid #e2e8f0; border-bottom-left-radius: 4px; }
.aicb-msg.user { align-self: flex-end; background: var(--aicb-primary); color: #fff; border-bottom-right-radius: 4px; }
.aicb-typing { align-self: flex-start; display: flex; gap: 4px; padding: 10px 12px; background: #fff; border: 1px solid #e2e8f0; border-radius: 14px; }
.aicb-typing span { width: 6px; height: 6px; border-radius: 50%; background: #94a3b8; animation: aicb-bounce 1s infinite; }
.aicb-typing span:nth-child(2) { animation-delay: .15s; }
.aicb-typing span:nth-child(3) { animation-delay: .3s; }
@keyframes aicb-bounce { 0%,80%,100% { transform: translateY(0); } 40% { transform: translateY(-4px); } }
#aicb-quick { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px 8px; background: #f8fafc; max-height: 110px; overflow-y: auto; flex: 0 0 auto; }
#aicb-quick[hidden] { display: none !important; }
.aicb-chip { border: 1px solid var(--aicb-primary); color: var(--aicb-primary); background: #fff; border-radius: 999px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
.aicb-chip:hover { background: var(--aicb-primary); color: #fff; }
#aicb-book { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; background: #fff; border-top: 1px solid #e2e8f0; flex: 0 0 auto; max-height: 62%; overflow-y: auto; }
#aicb-book[hidden] { display: none !important; }
#aicb-book input { border: 1px solid #cbd5e1; border-radius: 8px; padding: 8px 10px; font-size: 13px; }
#aicb-book-slot { margin: 0; font-size: 12px; color: #334155; }
.aicb-book-actions { display: flex; gap: 8px; }
.aicb-book-actions button { flex: 1; border: 0; border-radius: 8px; padding: 8px; cursor: pointer; font-weight: 600; }
.aicb-book-actions button[type=submit] { background: var(--aicb-primary); color: #fff; }
#aicb-book-cancel { background: #e2e8f0; color: #0f172a; }
#aicb-form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #e2e8f0; background: #fff; }
#aicb-input { flex: 1; border: 1px solid #cbd5e1; border-radius: 10px; padding: 10px 12px; font-size: 14px; }
#aicb-send { background: var(--aicb-primary); color: #fff; border: 0; border-radius: 10px; padding: 10px 14px; cursor: pointer; font-weight: 600; }
#aicb-root[hidden] { display: none !important; }
#aicb-panel[hidden] { display: flex !important; }
@media (max-width: 480px) {
  #aicb-root { right: 12px; bottom: 12px; }
  #aicb-panel { width: calc(100vw - 24px); height: calc(100vh - 96px); bottom: 72px; }
}
CSS;
}

function aicb_js() {
    return <<<'JS'
(function () {
  var root = document.getElementById('aicb-root');
  if (!root) return;
  root.hidden = false;

  var LS_KEY = 'aicb_state_v1';
  var ajax = root.getAttribute('data-ajax');
  var nonce = root.getAttribute('data-nonce');
  var launcher = document.getElementById('aicb-launcher');
  var panel = document.getElementById('aicb-panel');
  var closeBtn = document.getElementById('aicb-close');
  var messagesEl = document.getElementById('aicb-messages');
  var quickEl = document.getElementById('aicb-quick');
  var form = document.getElementById('aicb-form');
  var input = document.getElementById('aicb-input');
  var bookForm = document.getElementById('aicb-book');
  var iconChat = document.getElementById('aicb-icon-chat');
  var iconClose = document.getElementById('aicb-icon-close');
  var pendingSlot = null;
  var state = loadState();

  function loadState() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (!raw) return { sessionId: '', messages: [] };
      var parsed = JSON.parse(raw);
      return {
        sessionId: parsed.sessionId || '',
        messages: Array.isArray(parsed.messages) ? parsed.messages : []
      };
    } catch (e) {
      return { sessionId: '', messages: [] };
    }
  }

  function saveState() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (e) {}
  }

  function scrollBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addMessage(role, text, persist) {
    var div = document.createElement('div');
    div.className = 'aicb-msg ' + (role === 'user' ? 'user' : 'bot');
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollBottom();
    if (persist !== false) {
      state.messages.push({ role: role, text: text });
      if (state.messages.length > 80) state.messages = state.messages.slice(-80);
      saveState();
    }
  }

  function setButtons(buttons) {
    quickEl.innerHTML = '';
    if (!buttons || !buttons.length) return;
    quickEl.hidden = false;
    buttons.forEach(function (btn) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'aicb-chip';
      b.textContent = btn.label;
      b.addEventListener('click', function () {
        if (btn.start) {
          pendingSlot = { start: btn.start, end: btn.end || '', label: btn.label };
          showBookForm();
          addMessage('user', 'I would like ' + btn.label);
          addMessage('bot', 'Great. Please confirm your details for ' + btn.label + '.');
          return;
        }
        sendMessage(btn.value || btn.label);
      });
      quickEl.appendChild(b);
    });
  }

  function showBookForm() {
    bookForm.hidden = false;
    quickEl.hidden = true;
    document.getElementById('aicb-book-slot').textContent = pendingSlot
      ? 'Selected: ' + pendingSlot.label
      : 'Select a time first.';
  }

  function hideBookForm() {
    bookForm.hidden = true;
    quickEl.hidden = false;
  }

  var typingEl = null;
  function showTyping(on) {
    if (on && !typingEl) {
      typingEl = document.createElement('div');
      typingEl.className = 'aicb-typing';
      typingEl.innerHTML = '<span></span><span></span><span></span>';
      messagesEl.appendChild(typingEl);
      scrollBottom();
    } else if (!on && typingEl) {
      typingEl.remove();
      typingEl = null;
    }
  }

  function openChat(open) {
    root.classList.toggle('is-open', open);
    launcher.setAttribute('aria-expanded', open ? 'true' : 'false');
    panel.hidden = false;
    iconChat.hidden = open;
    iconClose.hidden = !open;
    if (open) { input.focus(); scrollBottom(); }
  }

  launcher.addEventListener('click', function () {
    openChat(!root.classList.contains('is-open'));
  });
  closeBtn.addEventListener('click', function () { openChat(false); });

  function proxy(endpoint, payload) {
    var body = new FormData();
    body.append('action', 'aicb_proxy');
    body.append('nonce', nonce);
    body.append('endpoint', endpoint);
    body.append('payload', JSON.stringify(payload || {}));
    return fetch(ajax, { method: 'POST', body: body, credentials: 'same-origin' })
      .then(function (res) { return res.json(); });
  }

  function sendMessage(text) {
    text = (text || '').trim();
    if (!text) return;
    addMessage('user', text);
    setButtons([]);
    input.value = '';
    showTyping(true);
    proxy('chat', { message: text, session_id: state.sessionId })
      .then(function (data) {
        showTyping(false);
        if (data && data.session_id) {
          state.sessionId = data.session_id;
          saveState();
        }
        if (data && data.reply) addMessage('bot', data.reply);
        else addMessage('bot', (data && data.error) || 'Sorry, something went wrong.');
        setButtons((data && data.buttons) || []);
      })
      .catch(function () {
        showTyping(false);
        addMessage('bot', 'I could not reach the assistant. Please try again in a moment.');
      });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    sendMessage(input.value);
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input.value);
    }
  });

  bookForm.addEventListener('submit', function (e) {
    e.preventDefault();
    if (!pendingSlot) {
      addMessage('bot', 'Please pick a time slot first.');
      return;
    }
    var name = document.getElementById('aicb-name').value.trim();
    var email = document.getElementById('aicb-email').value.trim();
    var phone = document.getElementById('aicb-phone').value.trim();
    var reason = document.getElementById('aicb-reason').value.trim();
    showTyping(true);
    proxy('book', {
      name: name,
      email: email,
      phone: phone,
      reason: reason,
      start: pendingSlot.start,
      end: pendingSlot.end,
      session_id: state.sessionId
    }).then(function (data) {
      showTyping(false);
      if (data && data.ok) {
        addMessage('bot', data.message || 'Your appointment is confirmed.');
        pendingSlot = null;
        hideBookForm();
        bookForm.reset();
        setButtons([{ label: 'Ask another question', value: 'Thanks, I have another question' }]);
      } else {
        addMessage('bot', (data && data.error) || 'Could not complete the booking. Please pick another time.');
      }
    }).catch(function () {
      showTyping(false);
      addMessage('bot', 'Could not complete the booking. Please try again.');
    });
  });
  document.getElementById('aicb-book-cancel').addEventListener('click', function () {
    pendingSlot = null;
    hideBookForm();
  });

  if (state.messages.length) {
    state.messages.forEach(function (m) { addMessage(m.role, m.text, false); });
  } else {
    addMessage('bot', 'Hi, I am ' + (document.getElementById('aicb-title').textContent || 'the assistant') + '. How can I help you today?', false);
    setButtons([
      { label: 'Book Appointment', value: 'I would like to book an appointment' },
      { label: 'Check my appointment', value: 'I would like to check my appointment' },
      { label: 'Our services', value: 'What services do you offer?' }
    ]);
  }
})();
JS;
}
