import {api, session} from './api.js';
import {CHANNELS, ROLES} from './labels.js';
import {brand, channelName, empty, lotLink, safe} from './ui.js';
import {lotsPage} from './pages/lots.js';
import {workbenchPage} from './pages/workbench.js';
import {reworkPage} from './pages/rework.js';
import {datasetsPage} from './pages/datasets.js';

const root = document.querySelector('#app');
const store = {lots:[], materials:[], detail:null, loading:true, error:'', zone:'front', filter:{search:'',state:''},
  rolls:[], packages:[], packageDetail:null, rollFilter:''};
const pages = {lots:'包装批次', workbench:'判级工作台', rework:'返工领料', datasets:'数据发包'};
let revision = 0;
let poll;
let feedbackTimer;

function currentRoute() {
  const url = new URL(location.href);
  const page = url.pathname.slice(1);
  return {page:Object.hasOwn(pages,page) ? page : 'lots', id:Number(url.searchParams.get('lot'))};
}

function navigate(href, replace = false) {
  history[replace ? 'replaceState' : 'pushState'](null, '', href);
  window.scrollTo(0, 0);
  load();
}

function loginScreen() {
  return `<main class="login-layout"><section class="login-visual"><div class="login-brand">${brand()}</div><div class="login-graphic"><div class="print-frame"><div class="reg-corner"></div><div class="print-bag"><span>禾 堤</span><i></i><strong>海盐燕麦</strong><b>脆片</b><span>SEA SALT / 120 G</span></div><div class="print-scale"><span>01</span><span>02</span><span>03</span><span>04</span><span>05</span></div></div></div><div class="login-foot"><span>品控中心 / 包装产线</span><span>VISION CELL 01—03</span></div></section>
    <section class="login-side"><div class="login-content"><p class="eyebrow">禾堤食品 / 车间系统</p><h1>包装外观<br>质检台</h1><p class="lede">登录当班岗位，接手包装批次。</p><form id="login-form"><label>岗位账号<input name="username" autocomplete="username" placeholder="输入岗位账号" required></label><label>口令<input name="password" type="password" autocomplete="current-password" placeholder="输入口令" required></label><button class="primary">登录工作台 <span>→</span></button><p id="login-error" role="alert"></p></form><div class="demo-login"><span>演示岗位</span>${Object.entries(ROLES).map(([key,label])=>`<button type="button" data-demo="${key}">${label}</button>`).join('')}</div></div></section></main>`;
}

function render() {
  const account = session.get();
  if (!account) {
    root.innerHTML = loginScreen();
    return;
  }
  const {page} = currentRoute();
  const body = store.loading ? '<p class="panel muted">读取批次记录…</p>' : store.error ? `${empty('暂时无法读取数据',store.error)}<button data-refresh>重新加载</button>` : {
    lots: () => lotsPage(store.lots,store.materials,account,store.filter),
    workbench: () => workbenchPage(store.lots,store.detail,account,store.zone),
    rework: () => reworkPage(store.lots,store.detail,account),
    datasets: () => datasetsPage(store,account),
  }[page]();
  root.innerHTML = `<div class="shell"><aside class="sidebar"><a href="/lots" data-nav class="brand-link">${brand()}</a><div class="side-content"><p class="sidebar-kicker">线边质检</p><nav aria-label="主导航">${Object.entries(pages).map(([route,label],index)=>`<a data-nav href="/${route}" ${route === page ? 'aria-current="page"' : ''}><span class="nav-icon icon-${index}" aria-hidden="true"></span>${label}</a>`).join('')}</nav></div><div class="side-footer"><span class="online"></span>视觉机台 VS-01–03</div></aside><div class="main-shell"><header class="topbar"><span>品控中心 <b>/</b> ${pages[page]}</span><div><span class="account-name">${safe(account.display_name)}</span><button data-logout>退出</button></div></header><main class="content" id="main">${body}</main></div></div><div id="feedback" role="status" aria-live="polite"></div>`;
}

async function load(quiet = false) {
  const tick = ++revision;
  clearTimeout(poll);
  if (!session.get()) {
    render();
    return;
  }
  const route = currentRoute();
  if (!quiet) {
    store.loading = true;
    store.error = '';
    render();
  }
  try {
    const [lots, materials] = await Promise.all([api.lots(), route.page === 'lots' ? api.materials() : []]);
    const eligible = route.page === 'rework' ? lots.filter(lot => lot.rework_state) : lots;
    const selected = route.id ? eligible.find(lot => lot.id === route.id) : eligible[0];
    const detail = route.page !== 'lots' && route.page !== 'datasets' && selected ? await api.detail(selected.id) : null;
    let rolls = store.rolls, packages = store.packages;
    if (route.page === 'datasets') {
      [rolls, packages] = await Promise.all([api.rolls(), api.packages()]);
    }
    if (tick !== revision) return;
    // 发包页没有批次详情，数据更新后必须重渲染，避免静默刷新不反映膜卷/包变化。
    const changed = !quiet || route.page === 'datasets'
      || store.detail?.lot.state !== detail?.lot.state || store.detail?.lot.id !== detail?.lot.id;
    const changedLot = store.detail?.lot.id !== detail?.lot.id;
    Object.assign(store,{lots,materials,detail,loading:false,error:'',rolls,packages});
    if (changedLot) {
      const live = detail?.marks.find(mark => mark.measure_id === detail.current?.id);
      store.zone = live?.zone || 'front';
    }
    if (changed) render();
    if (detail?.lot.state === 'queued') poll = setTimeout(() => load(true), 1000);
  } catch (error) {
    if (tick !== revision) return;
    Object.assign(store,{loading:false,error:error.message});
    render();
  }
}

function notify(text, kind = 'success') {
  const notice = root.querySelector('#feedback');
  if (!notice) return;
  clearTimeout(feedbackTimer);
  notice.className = `toast ${kind}`;
  notice.textContent = text;
  feedbackTimer = setTimeout(() => { notice.className = ''; notice.textContent = ''; }, 6500);
}

const numeric = new Set(['units','x_pct','y_pct','rework_units','replaced_units',...CHANNELS.map(([key])=>key)]);
function serialize(form, submitter) {
  const entries = new FormData(form, submitter);
  return Object.fromEntries([...entries].flatMap(([key,value]) => {
    if (!numeric.has(key)) return [[key,value]];
    return value === '' ? [] : [[key,Number(value)]];
  }));
}

const success = {measurements:'测量归档成功', marks:'缺陷已标记', grade:'判级已提交', 'grade/review':'等级已签核', 'rework/issue':'领料已过账', 'rework/verify':'复测已签核', close:'批次已关闭'};
async function perform(action, button, payload) {
  if (button) button.disabled = true;
  try {
    if (action === 'register') {
      const lot = await api.register(payload);
      navigate(lotLink('workbench',lot.id));
    } else {
      await api.action(store.detail.lot.id, action, payload);
      await load();
      notify(success[action] || '缺陷复核已保存');
    }
  } catch (error) {
    const message = `${error.message}${error.channels?.length ? ' · '+error.channels.map(channelName).join('、') : ''}`;
    const dialog = button?.closest('dialog');
    if (dialog?.isConnected) {
      dialog.querySelector('.form-error')?.remove();
      const feedback = document.createElement('p');
      feedback.className = 'notice warn form-error';
      feedback.setAttribute('role','alert');
      feedback.textContent = message;
      dialog.querySelector('form').prepend(feedback);
    } else notify(message, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

root.addEventListener('click', event => {
  const target = event.target.closest('a,button');
  if (!target) return;
  if (target.hasAttribute('data-nav') && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
    event.preventDefault();
    navigate(target.getAttribute('href'));
  }
  if (target.hasAttribute('data-logout')) {
    session.clear();
    navigate('/login');
  }
  if (target.dataset.demo) {
    const form = root.querySelector('#login-form');
    form.elements.username.value = target.dataset.demo;
    form.elements.password.value = 'PackDemo2026!';
    form.querySelector('button').focus();
  }
  if (target.dataset.dialog) {
    const dialog = root.querySelector(`#${target.dataset.dialog}`);
    if (!dialog) return;
    const form = dialog.querySelector('form');
    if (target.dataset.measure && form) {
      form.elements.measure_id.value = target.dataset.measure;
      form.elements.sample_view.value = target.dataset.sample || '';
      if (form.elements.roll_view) form.elements.roll_view.value = target.dataset.roll || '';
      if (form.elements.roll_code) form.elements.roll_code.value = '';
      if (form.elements.reason) form.elements.reason.value = '';
    }
    form?.querySelector('.form-error')?.toggleAttribute('hidden', true);
    dialog.showModal();
  }
  if (target.dataset.dismiss) root.querySelector(`#${target.dataset.dismiss}`).close();
  if (target.dataset.zone) {
    store.zone = target.dataset.zone;
    render();
    root.querySelector(`button[data-zone="${store.zone}"]`).focus();
  }
  if (target.hasAttribute('data-refresh')) load();
  if (target.dataset.action) perform(target.dataset.action,target);
});

root.addEventListener('change', event => {
  const target = event.target;
  if (target.dataset.lotPicker) navigate(lotLink(target.dataset.lotPicker,target.value));
  if (target.hasAttribute('data-grade')) {
    const fields = target.form.querySelector('[data-rework-fields]');
    const needsRework = target.value !== 'A' || target.value !== target.dataset.model;
    fields.hidden = !needsRework;
    for (const input of fields.querySelectorAll('input')) {
      input.disabled = !needsRework;
      input.required = needsRework;
    }
  }
});

root.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  const button = event.submitter;
  const payload = serialize(form,button);
  if (form.id === 'login-form') {
    button.disabled = true;
    try {
      session.save(await api.login(payload));
      navigate(location.pathname === '/login' ? '/lots' : location.pathname + location.search, true);
    } catch (error) {
      form.querySelector('#login-error').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  } else if (form.id === 'lot-filter') {
    store.filter = {...payload,search:payload.search.trim()};
    render();
  } else if (form.dataset.action) {
    await perform(form.dataset.action,button,payload);
  }
});
window.addEventListener('popstate', () => load());
window.addEventListener('session-expired', () => navigate('/login',true));

// ----------------------------- 数据发包页交互 ----------------------------- //
function trainingNotify(message, kind = 'error') {
  notify(message, kind);
}

root.addEventListener('click', async event => {
  const target = event.target.closest('button,a');
  if (!target) return;
  if (target.hasAttribute('data-package-close')) {
    root.querySelector('#package-dialog')?.close();
  }
  if (target.dataset.packageOpen) {
    try {
      store.packageDetail = await api.packageDetail(Number(target.dataset.packageOpen));
      render();
      root.querySelector('#package-dialog')?.showModal();
    } catch (error) { trainingNotify(error.message); }
  }
  if (target.dataset.packageLink) {
    const button = target;
    button.disabled = true;
    try {
      const link = await api.packageLink(Number(target.dataset.packageLink));
      const snapshot = await api.packageDownload(link.id, link.download.token);
      const blob = new Blob([JSON.stringify(snapshot, null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = `${link.code}.json`; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify('下载链接已签发，快照开始下载（24 小时内有效）', 'success');
    } catch (error) { trainingNotify(error.message); }
    finally { button.disabled = false; }
  }
});

root.addEventListener('submit', async event => {
  const form = event.target;
  if (form.id === 'roll-filter') {
    event.preventDefault();
    store.rollFilter = new FormData(form).get('search').trim();
    render();
    return;
  }
  if (form.id === 'publish-form') {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const entries = Object.fromEntries(new FormData(form));
      const ids = String(entries.measure_ids || '').split(/[\s,，]+/).filter(Boolean).map(Number).filter(Boolean);
      const payload = ids.length
        ? {purpose: entries.purpose, measure_ids: ids}
        : {purpose: entries.purpose, measure_ids: store.rolls.filter(r => r.usage_id && r.final_grade && !r.revoked_at).map(r => r.measure_id)};
      if (!payload.measure_ids.length) { trainingNotify('没有可入包的样本：缺卷号先不入包'); return; }
      const result = await api.publishPackage(payload);
      notify(result.retried ? '重试命中原活跃包，未新增训练包' : `训练包 ${result.code} 已发出`, 'success');
      await load(true);
    } catch (error) {
      trainingNotify(error.message);
    } finally { button.disabled = false; }
    return;
  }
  const action = form.dataset.trainingAction;
  if (!action) return;
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const payload = Object.fromEntries(new FormData(form));
    payload.measure_id = Number(payload.measure_id);
    const result = await api[action](payload);
    root.querySelector('dialog[open]')?.close();
    const stopped = result.deactivated_packages;
    notify(stopped?.length ? `操作成功，已停用 ${stopped.length} 个关联训练包，旧链接失效` : '操作成功', 'success');
    store.packageDetail = null;
    await load(true);
  } catch (error) {
    const feedback = form.querySelector('.form-error');
    if (feedback) { feedback.hidden = false; feedback.textContent = error.message; }
    else trainingNotify(error.message);
  } finally { button.disabled = false; }
});

root.addEventListener('close', event => {
  if (event.target.id === 'package-dialog') store.packageDetail = null;
});

load();
