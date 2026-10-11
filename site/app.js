// 稳健再平衡追踪页
// fetch history.json from the same repo (relative URL works on GitHub Pages).

async function main() {
  let history = [];
  try {
    const res = await fetch('data/history.json', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    history = await res.json();
  } catch (e) {
    document.getElementById('status-badge').innerHTML =
      `<span class="badge badge-err">⚠ 加载失败: ${e.message}</span>`;
    document.getElementById('curve').textContent = '';
    return;
  }

  if (!Array.isArray(history) || history.length === 0) {
    document.getElementById('status-badge').innerHTML =
      `<span class="badge badge-info">暂无数据</span>` +
      `<span class="muted">(GitHub Action 还没跑过, 或 <code>data/history.json</code> 为空)</span>`;
    document.getElementById('curve').textContent = '';
    return;
  }

  history.sort((a, b) => a.date.localeCompare(b.date));
  const latest = history[history.length - 1];

  renderStatus(latest);
  renderCurve(history);
  renderLatest(latest);
  renderTable(history);
}

function renderStatus(rec) {
  const cls = rec.triggered ? 'badge-warn' : 'badge-ok';
  const txt = rec.triggered ? '⚠ 需要调仓' : '✓ 未触发';
  document.getElementById('status-badge').innerHTML =
    `<span class="badge ${cls}">${txt}</span> ` +
    `<span>${rec.date}</span> ` +
    `<span>总市值 <strong>¥${Math.round(rec.total).toLocaleString()}</strong></span> ` +
    `<span class="muted">(ETF ¥${Math.round(rec.etf_value).toLocaleString()} + 现金 ¥${Math.round(rec.cash).toLocaleString()})</span>`;
}

function renderCurve(history) {
  const chart = echarts.init(document.getElementById('curve'));
  const dates = history.map(r => r.date);
  const totals = history.map(r => r.total);

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        const p = params[0];
        const rec = history[p.dataIndex];
        let html = `<b>${p.axisValue}</b><br/>` +
                   `总市值: ¥${Math.round(rec.total).toLocaleString()}<br/>`;
        rec.positions.forEach((pos, i) => {
          const w = (rec.weights[i] * 100).toFixed(2);
          html += `${pos.name}: ${w}%<br/>`;
        });
        html += `信号: ${rec.triggered ? '⚠ 触发' : '✓ 未触发'}`;
        return html;
      },
    },
    grid: { left: 60, right: 30, top: 40, bottom: 60 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { rotate: 30 },
    },
    yAxis: {
      type: 'value',
      name: '总市值 (¥)',
      scale: true,
      axisLabel: {
        formatter: (v) => v.toLocaleString(),
      },
    },
    series: [{
      name: '总市值',
      type: 'line',
      data: totals,
      smooth: true,
      showSymbol: false,
      areaStyle: { opacity: 0.15, color: '#c29438' },
      lineStyle: { width: 2, color: '#c29438' },
      itemStyle: { color: '#c29438' },
      markPoint: {
        symbol: 'pin',
        data: history
          .map((r, i) => r.triggered ? { coord: [dates[i], totals[i]], itemStyle: { color: '#c0392b' } } : null)
          .filter(x => x),
      },
    }],
  });

  window.addEventListener('resize', () => chart.resize());
}

function renderLatest(rec) {
  const rows = rec.positions.map((p, i) => {
    const w = rec.weights[i];
    const delta = (w - rec.target_w) * 100;
    const over = Math.abs(delta) > rec.threshold_pp;
    return `<tr>
      <td>${p.name}</td>
      <td>${p.code}</td>
      <td>${Math.round(p.shares).toLocaleString()}</td>
      <td>¥${p.price.toFixed(3)}</td>
      <td>¥${Math.round(p.value).toLocaleString()}</td>
      <td>${(w * 100).toFixed(2)}%</td>
      <td class="${over ? 'warn' : ''}">${delta >= 0 ? '+' : ''}${delta.toFixed(2)}pp</td>
    </tr>`;
  }).join('');

  document.getElementById('latest-detail').innerHTML = `
    <p class="muted">价格日期: ${rec.positions[0]?.date || '?'}</p>
    <table>
      <thead>
        <tr>
          <th>名称</th><th>代码</th><th>数量</th><th>价格</th><th>市值</th><th>权重</th><th>偏离</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderTable(history) {
  const rows = history.slice().reverse().map(r =>
    `<tr>
      <td>${r.date}</td>
      <td>¥${Math.round(r.total).toLocaleString()}</td>
      <td class="${r.triggered ? 'warn' : 'ok'}">${r.triggered ? '⚠ 触发' : '✓'}</td>
    </tr>`
  ).join('');
  document.getElementById('history-table').innerHTML = `
    <table>
      <thead>
        <tr><th>日期</th><th>总市值</th><th>信号</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

main();