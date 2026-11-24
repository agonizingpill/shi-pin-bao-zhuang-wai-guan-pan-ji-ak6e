import {DEACT_REASONS, GRADES, PACKAGE_PURPOSES, STATES} from '../labels.js';
import {empty, heading, number, options, safe, sectionTitle, status, time} from '../ui.js';

function rollBadge(roll) {
  if (!roll?.usage_id) return '<span class="chip warn">缺卷号 · 不入包</span>';
  return `<span class="chip ${safe(roll.side || '')}">${safe(roll.roll_code)}${roll.side ? ` · ${STATES[roll.side]}` : ''}</span>`;
}

function measureRow(row, account) {
  const signed = row.final_grade;
  const eligible = row.usage_id && signed && !row.revoked_at;
  return `<tr class="${row.revoked_at ? 'revoked' : ''}">
    <td><b class="mono">${safe(row.sample)}</b><small>v${row.revision} · ${safe(row.lot_code)}</small></td>
    <td>${rollBadge(row)}</td>
    <td>${signed ? `<b class="grade-inline">${safe(signed)}</b><small>${GRADES[signed] || ''}</small>` : '<span class="muted">未终判</span>'}</td>
    <td>${row.revoked_at ? `<span class="chip warn">已撤销</span><small>${safe(row.revoke_reason || '')}</small>`
      : eligible ? '<span class="chip train">可入包</span>' : '<span class="chip warn">不可入包</span>'}</td>
    <td class="row-actions">
      ${!row.usage_id && !row.revoked_at && account.role === 'operator'
        ? `<button data-dialog="roll" data-measure="${row.measure_id}" data-sample="${safe(row.sample)}">登记膜卷</button>` : ''}
      ${row.usage_id && account.role === 'supervisor'
        ? `<button data-dialog="correct" data-measure="${row.measure_id}" data-sample="${safe(row.sample)}" data-roll="${safe(row.roll_code)}">更正卷号</button>` : ''}
      ${!row.revoked_at && account.role === 'supervisor'
        ? `<button data-dialog="withdraw" data-measure="${row.measure_id}" data-sample="${safe(row.sample)}">撤销样本</button>` : ''}
    </td></tr>`;
}

function packageCard(pkg, account) {
  const inactive = pkg.state === 'inactive';
  return `<article class="package-card ${safe(pkg.state)}">
    <header><b class="mono">${safe(pkg.code)}</b>${status(pkg.state)}<span class="chip ${safe(pkg.purpose)}">${PACKAGE_PURPOSES[pkg.purpose]}</span></header>
    <div class="package-meta"><span>${number(pkg.item_count)} 个样本</span><span>${number(pkg.roll_count)} 个膜卷</span><small>${time(pkg.created_at)}</small></div>
    ${inactive ? `<p class="notice warn">停用原因：${DEACT_REASONS[pkg.deactivation_reason] || '—'}<br>${safe(pkg.deactivation_detail || '')}</p>` : ''}
    <footer>
      <button data-package-open="${pkg.id}">查包内样本/膜卷</button>
      ${!inactive ? `<button class="primary" data-package-link="${pkg.id}">签发下载链接</button>`
        : '<span class="muted">旧链接已不可下载</span>'}
    </footer></article>`;
}

export function datasetsPage(state, account) {
  const {rolls, packages, packageDetail: detail, rollFilter} = state;
  const filtered = rolls.filter(row => !rollFilter ||
    `${row.sample} ${row.lot_code} ${row.roll_code || ''}`.toLowerCase().includes(rollFilter.toLowerCase()));
  const eligibleCount = rolls.filter(row => row.usage_id && row.final_grade && !row.revoked_at).length;
  return `${heading('品控中心 / 训练交接', '数据发包', '人工终判作标签，按膜卷互斥切分训练与留出；缺卷号先不入包。')}
    <div class="ledger-summary"><div class="summary-head"><b>发包概况</b><small>HANDOVER</small></div>
      <div><span>已登记样本</span><strong>${String(rolls.filter(r => r.usage_id).length).padStart(2, '0')}</strong></div>
      <div><span>可入包样本</span><strong>${String(eligibleCount).padStart(2, '0')}</strong></div>
      <div><span>活跃训练包</span><strong>${String(packages.filter(p => p.state === 'active' && p.purpose === 'train').length).padStart(2, '0')}</strong></div>
      <div><span>已停用包</span><strong>${String(packages.filter(p => p.state === 'inactive').length).padStart(2, '0')}</strong></div></div>

    <div class="dataset-grid">
      <section class="panel ledger">${sectionTitle('膜卷登记台账', `${filtered.length} / ${rolls.length} 个样本`)}
        <form id="roll-filter" class="filter"><label>查找样本/膜卷<input name="search" placeholder="样本号 / 批号 / 卷号" value="${safe(rollFilter || '')}"></label><button>筛选</button></form>
        ${filtered.length ? `<div class="scroll-x"><table><thead><tr><th>样本 / 批次</th><th>膜卷</th><th>人工终判</th><th>入包资格</th><th></th></tr></thead>
          <tbody>${filtered.map(row => measureRow(row, account)).join('')}</tbody></table></div>`
          : empty('没有匹配的样本', '先在判级工作台完成签核，再登记膜卷。')}
        <footer class="table-footer">同批用过的膜卷只能归同一边；训练与留出不共用膜卷。</footer>
      </section>

      <aside class="panel publish-panel">${sectionTitle('发出数据包')}
        ${account.role === 'supervisor' ? `<form class="fields" id="publish-form">
          <label>用途<select name="purpose">${options(PACKAGE_PURPOSES)}</select></label>
          <label class="full">样本 ID 列表（逗号或空格分隔，留空=全部合格样本）
            <textarea name="measure_ids" rows="3" placeholder="例如 1,2,3；中断恢复时重试原集合"></textarea></label>
          <button class="primary full">发包 / 重试恢复</button>
          <p class="muted">同一批样本集合重试不会新增训练包；停用后需重新组包。</p>
        </form>` : '<p class="notice">仅质检主管可发包、更正卷号与撤销样本。</p>'}
      </aside>
    </div>

    <section class="panel">${sectionTitle('训练包与留出包', `${packages.length} 个`)}
      ${packages.length ? `<div class="package-grid">${packages.map(pkg => packageCard(pkg, account)).join('')}</div>`
        : empty('尚未发包', '主管选择已签核且有膜卷的样本后发出第一个训练包。')}
    </section>

    ${detail ? packageDetailDialog(detail) : ''}
    ${account.role === 'operator' ? rollDialog() : ''}
    ${account.role === 'supervisor' ? `${correctDialog()}${withdrawDialog()}` : ''}`;
}

function packageDetailDialog(detail) {
  const {package: pkg, items, rolls} = detail;
  return `<dialog class="package-dialog" id="package-dialog">
    <div class="dialog-heading"><h2>${safe(pkg.code)} · ${PACKAGE_PURPOSES[pkg.purpose]}</h2>
      <button data-package-close aria-label="关闭">×</button></div>
    ${pkg.state === 'inactive' ? `<p class="notice warn">已停用：${DEACT_REASONS[pkg.deactivation_reason] || '—'} — ${safe(pkg.deactivation_detail || '')}</p>` : ''}
    <h3>膜卷（${rolls.length}）</h3>
    <div class="roll-tags">${rolls.map(r => `<span class="chip ${safe(pkg.purpose)}">${safe(r.roll_code)} · ${number(r.samples)} 样本</span>`).join('')}</div>
    <h3>包内样本（${items.length}）</h3>
    <div class="scroll-x"><table><thead><tr><th>样本</th><th>批次</th><th>膜卷</th><th>标签</th><th>状态</th></tr></thead>
      <tbody>${items.map(item => `<tr><td class="mono">${safe(item.sample)}</td><td>${safe(item.lot_code)}</td>
        <td class="mono">${safe(item.roll_code)}</td><td><b class="grade-inline">${safe(item.final_grade || '—')}</b></td>
        <td>${item.revoked_at ? '<span class="chip warn">已撤销</span>' : '正常'}</td></tr>`).join('')}</tbody></table></div>
  </dialog>`;
}

const rollDialog = () => `<dialog id="roll"><div class="dialog-heading"><h2>登记膜卷</h2><button data-dismiss="roll" aria-label="关闭">×</button></div>
  <form class="fields" data-training-action="registerRoll"><input type="hidden" name="measure_id">
    <label class="full">样本<output name="sample_view"></output></label>
    <label class="full">膜卷号<input name="roll_code" required maxlength="64" placeholder="例如 RL-261121-07"></label>
    <button class="primary full">登记膜卷</button><p class="form-error notice warn" hidden></p></form></dialog>`;

const correctDialog = () => `<dialog id="correct"><div class="dialog-heading"><h2>更正卷号</h2><button data-dismiss="correct" aria-label="关闭">×</button></div>
  <form class="fields" data-training-action="correctRoll"><input type="hidden" name="measure_id">
    <label class="full">样本<output name="sample_view"></output></label>
    <label class="full">当前卷号<output name="roll_view"></output></label>
    <label class="full">新卷号<input name="roll_code" required maxlength="64"></label>
    <p class="notice warn">更正会停用包含该样本的活跃训练包，旧下载链接立即失效。</p>
    <button class="primary full">提交更正</button><p class="form-error notice warn" hidden></p></form></dialog>`;

const withdrawDialog = () => `<dialog id="withdraw"><div class="dialog-heading"><h2>撤销样本</h2><button data-dismiss="withdraw" aria-label="关闭">×</button></div>
  <form class="fields" data-training-action="withdrawSample"><input type="hidden" name="measure_id">
    <label class="full">样本<output name="sample_view"></output></label>
    <label class="full">撤销原因<textarea name="reason" required minlength="2" maxlength="300" rows="3"></textarea></label>
    <p class="notice warn">撤销会停用包含该样本的活跃训练包，旧下载链接立即失效。</p>
    <button class="primary full">确认撤销</button><p class="form-error notice warn" hidden></p></form></dialog>`;
