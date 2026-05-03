// Shared sidebar/topbar HTML for mockups
window.renderShell = function(opts) {
  const { active, breadcrumb, content } = opts;
  const items = [
    { key: 'maestros', label: 'Maestros', icon: 'M3 5a2 2 0 012-2h14a2 2 0 012 2v3H3V5zm0 5h18v3H3v-3zm0 5h18v3a2 2 0 01-2 2H5a2 2 0 01-2-2v-3z' },
    { key: 'planificacion', label: 'Planificación', icon: 'M7 2v4M17 2v4M3 8h18M5 4h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2z' },
    { key: 'operacion', label: 'Operación', icon: 'M3 17l4-4 4 4 7-7M21 17h-4v-4' },
    { key: 'seguimiento', label: 'Seguimiento IA', icon: 'M12 2a4 4 0 014 4v1h1a3 3 0 013 3v8a3 3 0 01-3 3H7a3 3 0 01-3-3v-8a3 3 0 013-3h1V6a4 4 0 014-4z' },
    { key: 'control', label: 'Control / Analítica', icon: 'M3 3v18h18M7 14v4M11 10v8M15 6v12M19 12v6' },
    { key: 'config', label: 'Configuración', icon: 'M12 8a4 4 0 100 8 4 4 0 000-8zm9 4l-2 .3-1 2.4 1.5 1.5-2 2-1.5-1.5-2.4 1L13 21h-2l-.3-2-2.4-1L6.5 19.5l-2-2L6 16l-1-2.4L3 13v-2l2-.3 1-2.4L4.5 6.5l2-2L8 6l2.4-1L11 3h2l.3 2 2.4 1L17.5 4.5l2 2L18 8l1 2.4 2 .3v1.3z' },
  ];
  const sidebar = `
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">VD</div>
        <div>
          <div class="title">ValueData × Falabella</div>
          <div class="subtitle">Operaciones · Logística</div>
        </div>
      </div>
      <nav class="nav">
        ${items.map(i => `
          <div class="nav-item ${active === i.key ? 'active' : ''}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${i.icon}"/></svg>
            <span>${i.label}</span>
          </div>
        `).join('')}
      </nav>
      <div style="margin-top: auto; padding: 16px; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted);">
        <div>Falabella Admin</div>
        <div style="opacity:.6;">prod · v2.4.1</div>
      </div>
    </aside>`;
  const topbar = `
    <header class="topbar">
      <div class="breadcrumb">${breadcrumb}</div>
      <div class="spacer"></div>
      <input class="search-input" placeholder="🔍  Buscar tracking, patente, cliente..." />
      <div class="icon-btn">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 01-3.4 0"/></svg>
        <span class="dot"></span>
      </div>
      <div class="avatar">GR</div>
    </header>`;
  document.body.innerHTML = `<div class="app">${sidebar}<div class="main">${topbar}<div class="content">${content}</div></div></div>`;
};
