/* 飞猪哨兵 · 客户端微交互 */

/* 0. 主题切换（auto / light / dark），偏好持久化到 localStorage */
(function initThemeToggle() {
    const STORAGE_KEY = 'fliggy.theme';

    function systemPref() {
        return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }

    function activeTheme() {
        // 返回用户当前感知到的"亮 / 暗"（auto 时跟随系统）
        const pref = (() => { try { return localStorage.getItem(STORAGE_KEY) || 'auto'; } catch (e) { return 'auto'; } })();
        return pref === 'auto' ? systemPref() : pref;
    }

    function applyTheme(pref) {
        const root = document.documentElement;
        if (pref === 'light' || pref === 'dark') {
            root.setAttribute('data-theme', pref);
        } else {
            root.removeAttribute('data-theme');  // auto：交给 media query
        }
        try { localStorage.setItem(STORAGE_KEY, pref); } catch (e) { /* private mode */ }

        // 同步按钮 active 状态（active = 用户偏好；不是感知到的亮暗）
        document.querySelectorAll('.theme-opt').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.themeSet === pref);
        });

        // 切换瞬间给整页一个过渡
        root.classList.add('theme-switching');
        setTimeout(() => root.classList.remove('theme-switching'), 250);
    }

    // 初始同步按钮状态（FOUC 脚本已设好 data-theme）
    function syncButtons() {
        let pref = 'auto';
        try { pref = localStorage.getItem(STORAGE_KEY) || 'auto'; } catch (e) { /* */ }
        document.querySelectorAll('.theme-opt').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.themeSet === pref);
        });
    }

    // 绑定点击
    document.querySelectorAll('.theme-opt').forEach((btn) => {
        btn.addEventListener('click', () => applyTheme(btn.dataset.themeSet));
    });

    // 系统偏好变化时，若用户选 auto 则实时跟随
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
        let pref = 'auto';
        try { pref = localStorage.getItem(STORAGE_KEY) || 'auto'; } catch (e) { /* */ }
        if (pref === 'auto') {
            // 不改 data-theme，但让按钮视觉保持 auto active（系统切换发生在 CSS media query 层）
            // 这里只重新 sync 一次，避免其他按钮被旧 active 卡住
            syncButtons();
        }
    });

    syncButtons();
})();

/* 1. 卖家行的关注 toggle（HTMX-style fetch；失败时回滚） */
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

/* 1b. 货架行的关注 toggle —— 通知后端「这个 cell 一旦出现非自营就推 webhook」 */
async function toggleShelfWatch(poiId, itemId, skuId, target) {
    const desired = !target.classList.contains('on');
    const orig = target.textContent.trim();
    target.classList.toggle('on', desired);
    target.textContent = desired ? '★' : '☆';
    try {
        const r = await fetch('/api/shelves/watch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ poi_id: poiId, item_id: itemId, sku_id: skuId, watched: desired }),
        });
        if (!r.ok) throw new Error('http ' + r.status);
    } catch (e) {
        target.classList.toggle('on', !desired);  // 回滚
        target.textContent = orig;
        toast('货架关注保存失败：' + e.message, 'err');
    }
}

/* 1c. POI 详情页"掉架的关注"区块 → 移除某个 SKU 的关注标记
   成功后 fadeOut 该行；如果所有掉架都被移除完了，刷新页面让 header counter 重新计算 */
async function removeDroppedWatch(poiId, itemId, skuId, btn) {
    const row = document.getElementById(`dropped-row-${itemId}-${skuId}`);
    if (!row) return;
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '移除中…';
    try {
        const r = await fetch('/api/shelves/watch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ poi_id: poiId, item_id: itemId, sku_id: skuId, watched: false }),
        });
        if (!r.ok) throw new Error('http ' + r.status);
        row.style.transition = 'opacity .25s';
        row.style.opacity = '0';
        setTimeout(() => {
            row.remove();
            // 区块空了 → 刷新整页让 dashboard tooltip / POI header counter 重算
            const tbody = document.querySelector('#dropped-watch-list tbody');
            if (tbody && tbody.children.length === 0) location.reload();
        }, 250);
        toast('✓ 已移除该掉架 SKU 的关注', 'ok');
    } catch (e) {
        btn.disabled = false;
        btn.textContent = origText;
        toast('移除失败：' + e.message, 'err');
    }
}

/* 2. 设置页 → 测试 webhook */
async function testWebhook(btn, kind) {
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '推送中…';
    try {
        const r = await fetch('/api/webhook/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind: kind || 'single' }),
        });
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