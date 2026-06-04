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
});

// ------------------------------------------------------------------ //
//  Toast notifications                                                 //
// ------------------------------------------------------------------ //

/**
 * Show a toast notification at the bottom-right of the screen.
 * @param {string} message - The message to display
 * @param {string} type - One of 'success', 'warning', 'danger', 'info'
 * @param {number} duration - How long to show the toast in ms (default 3000)
 */
function showToast(message, type, duration) {
  type = type || 'info';
  duration = duration || 3000;

  // Ensure the toast container exists
  var container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  var toast = document.createElement('div');
  toast.className = 'toast toast-' + type;
  toast.textContent = message;
  container.appendChild(toast);

  // Auto-remove after duration
  setTimeout(function() {
    if (toast.parentNode) {
      toast.parentNode.removeChild(toast);
    }
  }, duration);
}

// ------------------------------------------------------------------ //
//  API action helper                                                   //
// ------------------------------------------------------------------ //

/**
 * Submit an API action via AJAX and show a toast notification.
 *
 * @param {string} mode - The SABnzbd API mode (e.g. 'delete', 'pause', 'resume')
 * @param {Object} params - Key-value pairs to send as form data
 * @param {Function} onSuccess - Callback on successful API response
 * @param {Object} options - Optional: { confirm: 'Are you sure?', successMsg: 'Deleted', errorMsg: 'Failed' }
 */
function apiAction(mode, params, onSuccess, options) {
  options = options || {};

  // Optional confirmation dialog
  if (options.confirm && !confirm(options.confirm)) {
    return;
  }

  // Build form data
  var formData = new FormData();
  formData.append('apikey', document.querySelector('meta[name="api-key"]')?.content || '');
  formData.append('mode', mode);
  for (var key in params) {
    if (params.hasOwnProperty(key)) {
      formData.append(key, params[key]);
    }
  }

  fetch('/api?mode=' + encodeURIComponent(mode), {
    method: 'POST',
    body: formData
  })
  .then(function(resp) { return resp.json(); })
  .then(function(data) {
    if (data.status === true) {
      showToast(options.successMsg || 'Action completed', 'success');
      if (onSuccess) onSuccess(data);
    } else {
      showToast(options.errorMsg || data.error || 'Action failed', 'danger');
    }
  })
  .catch(function(err) {
    showToast('Network error: ' + err.message, 'danger');
  });
}

/**
 * Remove a table row from the DOM with a fade-out animation.
 * @param {HTMLElement} row - The <tr> element to remove
 */
function removeRow(row) {
  if (!row) return;
  row.style.transition = 'opacity 0.3s';
  row.style.opacity = '0';
  setTimeout(function() {
    if (row.parentNode) row.parentNode.removeChild(row);
    // If the table is now empty, show the empty state
    var tbody = row.parentNode || document.querySelector('.queue-table tbody');
    if (tbody && tbody.children.length === 0) {
      window.location.reload();
    }
  }, 300);
}