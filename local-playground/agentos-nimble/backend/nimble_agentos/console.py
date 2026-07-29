"""The Nimble product shell: a visible configuration surface beside the official UI.

The official Agent UI is the execution surface and is not modified. This module
renders the *shell around it*: Nimble branding, the run-control panel, the
resolved-configuration readout, and the wire inspector. The official UI is
embedded from the same origin, so the session cookie applies to it unchanged.

Design intent, stated so it is not lost in the markup:

* **Effort is rendered, not offered.** It appears as a disabled field reading
  ``low`` with an explanatory note. There is no select, and the control-plane
  API accepts no effort value, so the UI cannot present a choice the server
  would honour.
* **The API key field is write-only.** It posts once over the protected origin
  and is cleared from the DOM immediately. It is never placed in
  ``localStorage``/``sessionStorage``, never put in a URL, and the server
  reports only which source a run would use.
* **Evidence is the wire, not a self-report.** The inspector shows the request
  body the Nimble SDK actually transmitted, so an operator can confirm that the
  control they changed reached Nimble rather than trusting a rendered summary.

Served as a single self-contained document on purpose: the spike should run with
one Python process and no frontend build step of its own.
"""

from __future__ import annotations

# An original placeholder mark drawn for this spike: three ascending strokes
# suggesting a rising signal, plus a locator dot. Deliberately generic and
# locally defined -- no Nimble brand asset is hotlinked or copied in.
NIMBLE_MARK_SVG = """
<svg viewBox="0 0 32 32" width="26" height="26" role="img" aria-label="Nimble playground mark">
  <rect x="3"  y="17" width="5" height="11" rx="2.5" fill="#5B8DEF"/>
  <rect x="11" y="11" width="5" height="17" rx="2.5" fill="#7AA7F5"/>
  <rect x="19" y="5"  width="5" height="23" rx="2.5" fill="#A8C6FA"/>
  <circle cx="27.5" cy="6" r="3" fill="#4ADE80"/>
</svg>
"""

_STYLE = """
:root{
  --bg:#0b0d11; --panel:#12151b; --panel-2:#171b23; --line:#242a35;
  --fg:#e6e9ef; --muted:#9aa4b6; --accent:#5B8DEF; --ok:#4ADE80;
  --warn:#F5A524; --danger:#F87171; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
.app{display:grid;grid-template-columns:minmax(360px,420px) 1fr;height:100vh}
.side{border-right:1px solid var(--line);overflow-y:auto;background:var(--panel);display:flex;flex-direction:column}
.stage{display:flex;flex-direction:column;min-width:0}
header.brand{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line)}
.brand h1{font-size:14px;margin:0;font-weight:650;letter-spacing:.2px}
.brand .sub{font-size:11px;color:var(--muted)}
.badge{font-size:10px;font-weight:700;letter-spacing:.6px;padding:3px 7px;border-radius:999px;text-transform:uppercase}
.badge.test{background:rgba(245,165,36,.15);color:var(--warn);border:1px solid rgba(245,165,36,.35)}
.badge.live{background:rgba(74,222,128,.13);color:var(--ok);border:1px solid rgba(74,222,128,.35)}
.badge.off{background:rgba(148,163,184,.12);color:var(--muted);border:1px solid var(--line)}
section{padding:14px 16px;border-bottom:1px solid var(--line)}
section h2{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);margin:0 0 10px}
label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}
input[type=text],input[type=password],select,textarea{
  width:100%;background:var(--panel-2);color:var(--fg);border:1px solid var(--line);
  border-radius:7px;padding:8px 10px;font-size:13px;font-family:inherit}
textarea{font-family:var(--mono);font-size:12px;resize:vertical;min-height:62px}
input:disabled{opacity:.75;cursor:not-allowed;color:var(--muted)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.radio{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--fg);margin:4px 0}
button{background:var(--accent);color:#0b0d11;border:0;border-radius:7px;padding:8px 13px;
  font-size:13px;font-weight:650;cursor:pointer;font-family:inherit}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.45;cursor:not-allowed}
.note{font-size:11px;color:var(--muted);margin-top:5px;line-height:1.45}
.pill{display:inline-block;font-family:var(--mono);font-size:11px;background:var(--panel-2);
  border:1px solid var(--line);border-radius:5px;padding:2px 6px;color:var(--fg)}
pre{background:#0a0c10;border:1px solid var(--line);border-radius:7px;padding:10px;
  overflow:auto;max-height:260px;font-family:var(--mono);font-size:11.5px;margin:8px 0 0}
iframe{border:0;width:100%;height:100%;background:var(--bg)}
.stagebar{display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--line);
  background:var(--panel);font-size:12px;color:var(--muted)}
.hide{display:none!important}
.toast{margin-top:9px;font-size:12px;padding:7px 9px;border-radius:6px;display:none}
.toast.ok{display:block;background:rgba(74,222,128,.1);color:var(--ok);border:1px solid rgba(74,222,128,.3)}
.toast.err{display:block;background:rgba(248,113,113,.1);color:var(--danger);border:1px solid rgba(248,113,113,.3)}
.kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;font-size:12px}
.kv span:first-child{color:var(--muted)}
.gate{margin:auto;text-align:center;padding:40px}
"""

_SCRIPT = r"""
const $ = (id) => document.getElementById(id);
// Same-origin fetch: the HttpOnly session cookie is attached automatically, so
// no token is ever held in JS, in storage, or in a URL.
const api = (path, init) => fetch(path, Object.assign({credentials:'same-origin'}, init||{}));

function toast(el, ok, msg){ el.className = 'toast ' + (ok?'ok':'err'); el.textContent = msg; }

function parseJsonField(el, name, errors){
  const raw = (el.value || '').trim();
  if(!raw) return undefined;
  try { return JSON.parse(raw); }
  catch(e){ errors.push(name + ': ' + e.message); return undefined; }
}

function identityMode(){
  const checked = document.querySelector('input[name=identity]:checked');
  return checked ? checked.value : 'auto_provision';
}

function syncIdentityInputs(){
  const mode = identityMode();
  $('agentIdWrap').classList.toggle('hide', mode !== 'existing_agent_id');
  $('agentNameWrap').classList.toggle('hide', mode !== 'named_agent');
}

// Effort renders from the server-supplied policy, so test mode (a fixed local
// policy) and live mode (an optional per-run override) can never be confused.
function renderEffort(policy){
  const select = $('effortSelect');
  const fixed  = $('effortFixed');
  const note   = $('effortNote');

  if(!policy.selectable){
    select.classList.add('hide');
    fixed.classList.remove('hide');
    fixed.value = policy.value;
    note.textContent =
      'Pinned to "' + policy.value + '" in this mode. ' + policy.rationale;
  } else {
    fixed.classList.add('hide');
    select.classList.remove('hide');
    select.innerHTML = '';
    for(const choice of policy.choices){
      const option = document.createElement('option');
      // null is the sentinel for "omit the field entirely".
      option.value = (choice === null ? '' : choice);
      option.textContent = (choice === null ? policy.default_label : choice);
      select.appendChild(option);
    }
    // "max" is offered rather than hidden, so an operator learns it exists and
    // how to get it. Selecting it produces an explicit engagement notice from
    // the server; it is never silently sent, and never quietly downgraded.
    if(policy.coming_soon){
      const option = document.createElement('option');
      option.value = policy.coming_soon;
      option.textContent = policy.coming_soon + ' — coming soon (contact Nimble)';
      select.appendChild(option);
    }
    select.value = policy.selected == null ? '' : policy.selected;
    note.textContent = policy.rationale;
  }

  const excluded = Object.entries(policy.excluded || {})
    .map(([tier, why]) => tier + ' — ' + why).join(' · ');
  $('effortExcluded').textContent = excluded ? ('Not selectable: ' + excluded) : '';
}

async function refreshSession(){
  const r = await api('/nimble/api/session');
  if(r.status === 401){ showGate(); return null; }
  const s = await r.json();
  renderSession(s);
  return s;
}

function renderSession(s){
  $('principal').textContent = s.principal;
  $('scopes').textContent = (s.scopes||[]).join(', ') || 'none';
  $('agentId').placeholder = 'wsa_...';
  const mode = s.runtime.run_mode;
  const badge = $('modeBadge');
  badge.textContent = mode === 'live' ? 'live' : 'test mode';
  badge.className = 'badge ' + (mode === 'live' ? 'live' : 'test');
  $('llm').textContent = s.runtime.llm;
  $('nimbleBase').textContent = s.runtime.nimble_base_url;
  $('pollInterval').textContent = s.poll.interval_seconds + 's';
  renderEffort(s.effort);

  const key = s.profile.nimble_key;
  $('keySource').textContent = key.source;
  $('keyDetail').textContent =
    key.session_override_present
      ? 'A per-session override is active and takes precedence over the shared server key.'
      : (key.shared_key_present
          ? 'No override set. Runs use the shared server-side key.'
          : 'No override and no shared server key. Runs will fail until one is provided.');
  $('clearKeyBtn').disabled = !key.session_override_present;

  $('resolved').textContent = s.resolved_run_config
    ? JSON.stringify(s.resolved_run_config, null, 2)
    : 'No run resolved yet. Send a message in the chat on the right, then refresh.';

  $('canRun').textContent = s.capabilities.can_run ? 'yes' : 'no (missing nimble:run scope)';
  $('wireWrap').classList.toggle('hide', mode === 'live');
}

function collectControls(){
  const errors = [];
  const mode = identityMode();
  const body = { enable_events: $('enableEvents').checked };

  if(mode === 'existing_agent_id'){
    const v = $('agentId').value.trim();
    if(v) body.agent_id = v; else errors.push('agent_id: required for the existing-agent mode');
  } else if(mode === 'named_agent'){
    const v = $('agentName').value.trim();
    if(v) body.agent_name = v; else errors.push('agent_name: required for the named-agent mode');
  }

  // Only sent when the mode allows selecting it; an empty value means
  // "omit", which is the default and preserves the agent/template default.
  const effortSelect = $('effortSelect');
  if(!effortSelect.classList.contains('hide') && effortSelect.value){
    body.effort = effortSelect.value;
  }

  const useCase = $('useCase').value;
  if(useCase) body.use_case = useCase;
  const skill = $('skill').value.trim();
  if(skill) body.skill = skill;

  const inputData = parseJsonField($('inputData'), 'input_data', errors);
  if(inputData !== undefined) body.input_data = inputData;
  const outputSchema = parseJsonField($('outputSchema'), 'output_schema', errors);
  if(outputSchema !== undefined) body.output_schema = outputSchema;
  const sources = parseJsonField($('sources'), 'sources', errors);
  if(sources !== undefined) body.sources = sources;

  return { body, errors };
}

async function saveControls(){
  const { body, errors } = collectControls();
  const el = $('controlsToast');
  if(errors.length){ toast(el, false, errors.join(' | ')); return; }
  const r = await api('/nimble/api/controls', {
    method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  if(!r.ok){
    let detail;
    try { detail = (await r.json()).detail; } catch(e) { detail = null; }
    // A coming-soon tier gets its full engagement notice surfaced, not a status
    // code. The whole point of rejecting rather than downgrading is that the
    // operator learns what to do next.
    if(detail && detail.code === 'effort_tier_coming_soon'){
      toast(el, false, detail.message + ' ' + detail.next_step +
        ' (policy: ' + detail.degradation_policy + ' — never silently downgraded)');
      return;
    }
    toast(el, false, 'Rejected (' + r.status + '): ' +
      (typeof detail === 'string' ? detail : JSON.stringify(detail || '')).slice(0,400));
    return;
  }
  renderSession(await r.json());
  toast(el, true, 'Controls saved. The next run builds NimbleAgentTools with these values.');
}

async function applyKey(){
  const field = $('apiKey');
  const el = $('keyToast');
  const value = field.value;
  if(!value.trim()){ toast(el, false, 'Enter a key, or use Clear to remove the override.'); return; }
  // Cleared synchronously, BEFORE the await. Clearing afterwards would leave the
  // plaintext readable via .value for the whole round trip -- the password input
  // masks it visually, not programmatically.
  field.value = '';
  const r = await api('/nimble/api/key', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({api_key:value})
  });
  if(!r.ok){ toast(el, false, 'Rejected (' + r.status + ')'); return; }
  renderSession(await r.json());
  toast(el, true, 'Override stored server-side for this principal. It is not kept in the browser.');
}

async function clearKey(){
  const r = await api('/nimble/api/key', {method:'DELETE'});
  const el = $('keyToast');
  if(!r.ok){ toast(el, false, 'Failed (' + r.status + ')'); return; }
  renderSession(await r.json());
  toast(el, true, 'Override cleared. Runs fall back to the shared server key.');
}

async function refreshWire(){
  const el = $('wireToast');
  const r = await api('/nimble/api/wire?limit=12');
  if(!r.ok){ toast(el, false, 'Unavailable (' + r.status + ')'); return; }
  const data = await r.json();
  const posts = (data.requests||[]).filter(x => x.method === 'POST');
  $('wire').textContent = posts.length
    ? posts.map(p => p.method + ' ' + p.path + '\n' + JSON.stringify(p.body, null, 2)).join('\n\n')
    : 'No run request captured yet. Send a message in the chat, then refresh.';
  toast(el, true, 'Showing what the Nimble SDK actually transmitted.');
}

function showGate(){ $('gate').classList.remove('hide'); $('app').classList.add('hide'); }
function showApp(){ $('gate').classList.add('hide'); $('app').classList.remove('hide'); }

async function signIn(readOnly){
  const r = await api('/__edge/login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({read_only: !!readOnly})
  });
  if(!r.ok){ toast($('gateToast'), false, 'Sign-in failed (' + r.status + ')'); return; }
  showApp();
  $('agentFrame').src = '__UI_PREFIX__';
  await refreshSession();
}

async function boot(){
  const r = await api('/__edge/whoami');
  if(r.status === 401){ showGate(); return; }
  showApp();
  $('agentFrame').src = '__UI_PREFIX__';
  await refreshSession();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input[name=identity]').forEach(el =>
    el.addEventListener('change', syncIdentityInputs));
  $('saveBtn').addEventListener('click', saveControls);
  $('applyKeyBtn').addEventListener('click', applyKey);
  $('clearKeyBtn').addEventListener('click', clearKey);
  $('wireBtn').addEventListener('click', refreshWire);
  $('refreshBtn').addEventListener('click', refreshSession);
  $('signInBtn').addEventListener('click', () => signIn(false));
  $('signInRoBtn').addEventListener('click', () => signIn(true));
  syncIdentityInputs();
  boot();
});
"""


def render_console(*, ui_prefix: str = "/ui") -> str:
    """Render the shell document."""
    script = _SCRIPT.replace("__UI_PREFIX__", ui_prefix)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Nimble x AgentOS playground</title>
<style>{_STYLE}</style>
</head>
<body>

<div id="gate" class="gate hide">
  <div style="display:flex;align-items:center;gap:10px;justify-content:center;margin-bottom:14px">
    {NIMBLE_MARK_SVG}<h1 style="margin:0;font-size:18px">Nimble &times; AgentOS playground</h1>
  </div>
  <p class="note" style="max-width:460px;margin:0 auto 18px">
    This playground sits behind a protected origin. Sign in to continue. Locally this stands in
    for Cloudflare Access; in production the edge authenticates before any request reaches AgentOS.
  </p>
  <div class="row" style="justify-content:center">
    <button id="signInBtn">Sign in (operator)</button>
    <button id="signInRoBtn" class="ghost">Sign in read-only</button>
  </div>
  <div id="gateToast" class="toast"></div>
</div>

<div id="app" class="app hide">
  <aside class="side">
    <header class="brand">
      {NIMBLE_MARK_SVG}
      <div style="flex:1">
        <h1>Nimble &times; AgentOS</h1>
        <div class="sub">Agent API V2 &middot; NimbleAgentTools</div>
      </div>
      <span id="modeBadge" class="badge off">…</span>
    </header>

    <section>
      <h2>Session</h2>
      <div class="kv"><span>principal</span><span class="pill" id="principal">…</span></div>
      <div class="kv"><span>scopes</span><span class="pill" id="scopes">…</span></div>
      <div class="kv"><span>can start runs</span><span class="pill" id="canRun">…</span></div>
      <div class="kv"><span>driver</span><span class="pill" id="llm">…</span></div>
      <div class="kv"><span>nimble base url</span><span class="pill" id="nimbleBase">…</span></div>
      <div class="kv"><span>status poll interval</span><span class="pill" id="pollInterval">…</span></div>
      <p class="note">
        The poll interval paces Nimble run-status checks only. Streaming (SSE) delivery is
        event-driven and is not throttled.
      </p>
    </section>

    <section>
      <h2>Agent identity</h2>
      <label class="radio"><input type="radio" name="identity" value="auto_provision" checked/>
        Agentless &mdash; Nimble auto-provisions a one-off agent</label>
      <label class="radio"><input type="radio" name="identity" value="existing_agent_id"/>
        Existing agent&nbsp;id</label>
      <label class="radio"><input type="radio" name="identity" value="named_agent"/>
        Named agent &mdash; create or reuse server-side</label>
      <div id="agentIdWrap" class="hide">
        <label for="agentId">agent_id</label>
        <input type="text" id="agentId" placeholder="wsa_..."/>
      </div>
      <div id="agentNameWrap" class="hide">
        <label for="agentName">agent_name</label>
        <input type="text" id="agentName" placeholder="my-research-agent"/>
      </div>
      <p class="note">agent_id and agent_name are mutually exclusive; the toolkit rejects both together.</p>
    </section>

    <section>
      <h2>Run configuration</h2>
      <label for="useCase">use_case</label>
      <select id="useCase">
        <option value="">(unset &mdash; inherit the agent's locked value)</option>
        <option value="research">research</option>
        <option value="enrichment">enrichment</option>
        <option value="dataset_building">dataset_building</option>
      </select>

      <label for="effort">effort</label>
      <!-- Both renderings exist; the server's policy decides which one is shown,
           so the console can never offer a tier this mode would not honour. -->
      <select id="effortSelect" class="hide"></select>
      <input type="text" id="effortFixed" value="low" disabled readonly/>
      <p class="note" id="effortNote">…</p>
      <p class="note" id="effortExcluded"></p>

      <label for="skill">skill &mdash; one-time instructions</label>
      <textarea id="skill" placeholder="Prefer official documentation and cite it."></textarea>

      <label for="inputData">input_data (JSON object or array of rows)</label>
      <textarea id="inputData" placeholder='[{{"company":"Nimble"}}]'></textarea>

      <label for="outputSchema">output_schema (JSON)</label>
      <textarea id="outputSchema" placeholder='{{"type":"object","properties":{{"answer":{{"type":"string"}}}}}}'></textarea>

      <label for="sources">sources (JSON: allow / block / avoid / prioritize)</label>
      <textarea id="sources" placeholder='{{"prioritize":"docs.nimbleway.com"}}'></textarea>

      <label class="radio" style="margin-top:12px">
        <input type="checkbox" id="enableEvents"/> enable_events &mdash; server-side run events
      </label>

      <div class="row" style="margin-top:12px">
        <button id="saveBtn">Save controls</button>
        <button id="refreshBtn" class="ghost">Refresh</button>
      </div>
      <div id="controlsToast" class="toast"></div>
    </section>

    <section>
      <h2>Nimble API key</h2>
      <div class="kv"><span>active source</span><span class="pill" id="keySource">…</span></div>
      <p class="note" id="keyDetail">…</p>
      <label for="apiKey">per-session override</label>
      <input type="password" id="apiKey" autocomplete="off" spellcheck="false" placeholder="paste key, applied once"/>
      <div class="row" style="margin-top:9px">
        <button id="applyKeyBtn">Apply override</button>
        <button id="clearKeyBtn" class="ghost">Clear</button>
      </div>
      <p class="note">
        Sent once over this protected origin and held in server memory for your principal only.
        It is never written to browser storage, never placed in a URL, and never returned by any endpoint.
      </p>
      <div id="keyToast" class="toast"></div>
    </section>

    <section>
      <h2>Resolved run configuration</h2>
      <p class="note">What the factory actually built for the most recent run on this principal.</p>
      <pre id="resolved">…</pre>
    </section>

    <section id="wireWrap">
      <h2>Wire inspector (test mode)</h2>
      <p class="note">
        The request body the Nimble SDK actually transmitted. This is the evidence that a control
        you changed reached Nimble &mdash; not a summary the server wrote about itself.
      </p>
      <div class="row"><button id="wireBtn" class="ghost">Refresh wire log</button></div>
      <pre id="wire">…</pre>
      <div id="wireToast" class="toast"></div>
    </section>
  </aside>

  <main class="stage">
    <div class="stagebar">
      <strong style="color:var(--fg);font-weight:600">Official Agno Agent UI</strong>
      <span>&mdash; unmodified upstream build, same origin, session cookie applies</span>
    </div>
    <iframe id="agentFrame" title="Official Agno Agent UI"></iframe>
  </main>
</div>

<script>{script}</script>
</body></html>
"""
