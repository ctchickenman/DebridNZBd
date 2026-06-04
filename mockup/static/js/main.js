/* DebridNZBd - Main JavaScript */

document.addEventListener('DOMContentLoaded', () => {
  // Toggle advanced settings
  document.querySelectorAll('.advanced-toggle').forEach(toggle => {
    toggle.addEventListener('click', (e) => {
      e.preventDefault();
      const cardBody = toggle.closest('.card-header')?.nextElementSibling;
      if (cardBody) {
        const advancedDiv = cardBody.querySelector('.advanced-settings');
        if (advancedDiv) {
          advancedDiv.classList.toggle('show');
          toggle.textContent = advancedDiv.classList.contains('show')
            ? 'Hide Advanced ▴'
            : 'Show Advanced ▾';
        }
      }
    });
  });

  // Confirm delete actions
  document.querySelectorAll('.btn-danger').forEach(btn => {
    btn.addEventListener('click', (e) => {
      if (!confirm('Are you sure you want to delete this item?')) {
        e.preventDefault();
      }
    });
  });

  // Auto-refresh queue on the home page
  if (window.location.pathname === '/' || window.location.pathname.endsWith('index.html')) {
    // In the real app, this would poll /api?mode=queue every 5 seconds
    console.log('Queue auto-refresh would be active in the real app');
  }
});