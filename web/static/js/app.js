/* 飞猪哨兵 · 客户端微交互 */

/* 1. SKU 行的关注 toggle（HTMX-style fetch；失败时回滚） */
async function toggleWatch(sellerId, target) {
    const desired = !target.classList.contains('on');
    target.classList.toggle('on', desired);
    try {
        const r = await fetch(`/api/sellers/${encodeURIComponent(sellerId)}/watch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ watched: desired }),
        });
        if (!r.ok) throw new Error('http ' + r.status);
    } catch (e) {
        target.classList.toggle('on', !desired);  // 回滚
        toast('关注状态保存失败：' + e.message, 'err');
    }
}

/* 2. 设置页 → 测试 webhook */
async function testWebhook(btn) {
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '推送中…';
    try {
        const r = await fetch('/api/webhook/test', { method: 'POST' });
        const j = await r.json();
        if (j.ok) {
            toast('✓ Webhook 已发送 (HTTP ' + j.status_code + ')', 'ok');
        } else {
            toast('✗ 推送失败：' + (j.error || ('HTTP ' + j.status_code + ' ' + j.response)), 'err');
        }
    } catch (e) {
        toast('请求失败：' + e.message, 'err');
    } finally {
        btn.disabled = false;
        btn.textContent = orig;
    }
}

/* 3. Toast */
function toast(msg, kind) {
    const t = document.createElement('div');
    t.className = 'toast toast--' + (kind || 'ok');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
}

/* 4. 自动消失的 ?saved=1 提示（设置页） */
window.addEventListener('DOMContentLoaded', () => {
    if (location.search.includes('saved=1')) {
        toast('✓ 已保存', 'ok');
        history.replaceState(null, '', location.pathname);
    }
});

/* 5. 数字滚动动画（KPI 卡片） */
window.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.kpi .value[data-counter]').forEach(el => {
        const target = parseInt(el.dataset.counter, 10) || 0;
        const dur = 600;
        const start = performance.now();
        const step = (now) => {
            const t = Math.min(1, (now - start) / dur);
            el.textContent = Math.round(target * (1 - Math.pow(1 - t, 3)));
            if (t < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
    });
});