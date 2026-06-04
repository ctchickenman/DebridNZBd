/* DebridNZBd - Directory Picker Modal */

let dirPickerModal = null;
let currentPath = '';
let currentRelative = '';
let currentAbsPath = '';

/**
 * Open the directory picker modal for a given input element.
 *
 * @param {HTMLInputElement} inputEl - The input element to update with the selected path.
 * @param {Object} options - Optional configuration.
 * @param {string} options.basePath - Base path to strip from relative paths (e.g. "downloads/complete").
 *                                   For category dir fields, this removes the complete_dir prefix
 *                                   so only the subdirectory name is stored.
 * @param {string} options.startPath - Absolute path to start browsing at.
 */
async function openDirectoryPicker(inputEl, options) {
  options = options || {};

  // Create modal if it doesn't exist
  if (!dirPickerModal) {
    createDirPickerModal();
  }

  // Store reference to the target input and options
  dirPickerModal._targetInput = inputEl;
  dirPickerModal._basePath = options.basePath || '';

  // Hide the create-folder row until user clicks the button
  var createRow = document.getElementById('dir-picker-create-row');
  if (createRow) createRow.style.display = 'none';

  // Show modal
  dirPickerModal.style.display = 'flex';

  // Load starting directory
  var startPath = options.startPath || '';
  if (startPath) {
    await loadDirectoryListing(startPath);
  } else {
    await loadDirectoryListing('');
  }
}

/**
 * Create the directory picker modal DOM elements.
 */
function createDirPickerModal() {
  var overlay = document.createElement('div');
  overlay.className = 'dir-picker-overlay';

  overlay.innerHTML =
    '<div class="dir-picker-modal">' +
      '<div class="dir-picker-header">' +
        '<h3>Browse Directories</h3>' +
        '<button class="dir-picker-close" onclick="closeDirectoryPicker()">&times;</button>' +
      '</div>' +
      '<div class="dir-picker-current-path" id="dir-picker-path"></div>' +
      '<div class="dir-picker-list" id="dir-picker-list"></div>' +
      '<div id="dir-picker-create-row" class="dir-picker-create" style="display:none;">' +
        '<input type="text" id="dir-picker-create-input" placeholder="New folder name..." ' +
          'class="dir-picker-create-input">' +
        '<button class="btn btn-sm btn-success" onclick="createNewFolder()" ' +
          'id="dir-picker-create-btn">Create</button>' +
        '<button class="btn btn-sm btn-secondary" onclick="cancelCreateFolder()">Cancel</button>' +
      '</div>' +
      '<div class="dir-picker-actions">' +
        '<button class="btn btn-secondary" onclick="closeDirectoryPicker()">Cancel</button>' +
        '<button class="btn btn-outline" onclick="showCreateFolder()" ' +
          'title="Create a new folder in the current directory">📁 New Folder</button>' +
        '<button class="btn btn-primary" onclick="selectCurrentDirectory()">Select This Directory</button>' +
      '</div>' +
    '</div>';

  // Close on overlay click
  overlay.addEventListener('click', function(e) {
    if (e.target === overlay) closeDirectoryPicker();
  });

  // Close on Escape key
  overlay.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeDirectoryPicker();
  });

  // Enter key in create folder input
  overlay.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && e.target.id === 'dir-picker-create-input') {
      createNewFolder();
    }
  });

  document.body.appendChild(overlay);
  dirPickerModal = overlay;
}

/**
 * Close the directory picker modal.
 */
function closeDirectoryPicker() {
  if (dirPickerModal) {
    dirPickerModal.style.display = 'none';
  }
}

/**
 * Show the create-folder input row.
 */
function showCreateFolder() {
  var createRow = document.getElementById('dir-picker-create-row');
  if (createRow) {
    createRow.style.display = 'flex';
    var input = document.getElementById('dir-picker-create-input');
    if (input) {
      input.value = '';
      input.focus();
    }
  }
}

/**
 * Hide the create-folder input row.
 */
function cancelCreateFolder() {
  var createRow = document.getElementById('dir-picker-create-row');
  if (createRow) createRow.style.display = 'none';
}

/**
 * Create a new folder in the current directory via the API,
 * then refresh the listing to show it.
 */
async function createNewFolder() {
  var input = document.getElementById('dir-picker-create-input');
  var btn = document.getElementById('dir-picker-create-btn');
  if (!input || !input.value.trim()) {
    showToast('Please enter a folder name', 'warning');
    return;
  }

  var folderName = input.value.trim();
  btn.disabled = true;
  btn.textContent = '...';

  try {
    var response = await fetch('/api/browse/mkdir', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: currentAbsPath, name: folderName})
    });
    var data = await response.json();

    if (response.ok && data.status !== false) {
      showToast('Folder "' + folderName + '" created', 'success');
      // Hide the create row and refresh the listing
      cancelCreateFolder();
      // Navigate into the newly created folder
      if (data.path) {
        await loadDirectoryListing(data.path);
      } else {
        await loadDirectoryListing(currentAbsPath);
      }
    } else {
      showToast(data.error || 'Failed to create folder', 'danger');
    }
  } catch (err) {
    showToast('Error creating folder: ' + err.message, 'danger');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Create';
  }
}

/**
 * Fetch and display a directory listing from the backend.
 *
 * @param {string} path - The path to browse. Empty string lists root directories.
 */
async function loadDirectoryListing(path) {
  var url = path ? '/api/browse?path=' + encodeURIComponent(path) : '/api/browse';

  try {
    var response = await fetch(url);
    if (!response.ok) {
      var data = null;
      try { data = await response.json(); } catch(e) {}
      showToast((data && data.error) ? data.error : 'Failed to browse directory', 'danger');
      return;
    }
    var data = await response.json();

    currentAbsPath = data.current_path;
    currentRelative = data.relative_path;

    // Update path display
    var pathEl = document.getElementById('dir-picker-path');
    if (pathEl) {
      pathEl.textContent = data.is_root ? 'Root Directories' : data.current_path;
    }

    // Build directory list
    var listEl = document.getElementById('dir-picker-list');
    if (!listEl) return;
    listEl.innerHTML = '';

    // Add "Up" navigation if parent exists
    if (data.parent !== null) {
      var upItem = document.createElement('div');
      upItem.className = 'dir-picker-item parent';
      upItem.textContent = '↑ .. (parent)';
      upItem.setAttribute('data-path', data.parent);
      upItem.onclick = function() {
        loadDirectoryListing(this.getAttribute('data-path'));
      };
      listEl.appendChild(upItem);
    }

    // Add directory entries
    for (var i = 0; i < data.directories.length; i++) {
      var dir = data.directories[i];
      var item = document.createElement('div');
      item.className = 'dir-picker-item';
      if (data.is_root) {
        item.textContent = dir.name + (dir.exists ? '' : ' (not created)');
      } else {
        item.textContent = dir.name;
      }
      item.setAttribute('data-path', dir.path);
      item.onclick = (function(p) {
        return function() { loadDirectoryListing(p); };
      })(dir.path);
      listEl.appendChild(item);
    }

    // Show empty state
    if (data.directories.length === 0 && !data.parent && data.is_root) {
      var empty = document.createElement('div');
      empty.className = 'dir-picker-empty';
      empty.textContent = 'No directories found.';
      listEl.appendChild(empty);
    } else if (data.directories.length === 0 && !data.is_root) {
      var empty = document.createElement('div');
      empty.className = 'dir-picker-empty';
      empty.textContent = 'No subdirectories. Click "Select This Directory" to use this folder.';
      listEl.appendChild(empty);
    }

    // Hide create row when navigating (user can re-open it)
    cancelCreateFolder();
  } catch (err) {
    showToast('Error browsing directory: ' + err.message, 'danger');
  }
}

/**
 * Select the current directory and update the target input field.
 *
 * Strips the basePath prefix from the relative path for category dir fields
 * (e.g. "downloads/complete/movies" becomes "movies").
 */
function selectCurrentDirectory() {
  if (!dirPickerModal || !dirPickerModal._targetInput) {
    closeDirectoryPicker();
    return;
  }

  var value = currentRelative || currentAbsPath;
  var basePath = dirPickerModal._basePath || '';

  // For category dir fields, strip the complete_dir prefix so
  // only the subdirectory name is stored (e.g. "movies" not "downloads/complete/movies")
  if (basePath && value) {
    // Normalize: ensure both use forward slashes and no trailing slash
    var normalizedBase = basePath.replace(/\/+$/, '');
    var normalizedValue = value.replace(/\/+$/, '');

    if (normalizedValue === normalizedBase) {
      // Selected the base directory itself — store empty string
      value = '';
    } else if (normalizedValue.startsWith(normalizedBase + '/')) {
      // Selected a subdirectory — strip the base prefix
      value = normalizedValue.substring(normalizedBase.length + 1);
    }
    // If the value doesn't start with the base, keep it as-is
    // (user browsed outside the base directory)
  }

  var inputEl = dirPickerModal._targetInput;
  inputEl.value = value;

  // Trigger change event for any listeners
  if (typeof Event !== 'undefined') {
    inputEl.dispatchEvent(new Event('change'));
  }

  closeDirectoryPicker();
}