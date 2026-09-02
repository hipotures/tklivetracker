/**
 * Users management functionality
 */

// Users page event listeners
TikTokRecorderApp.prototype.setupUsersEventListeners = function() {
    // Users page controls
    const usersSearch = document.getElementById('users-search');
    const clearSearchBtn = document.getElementById('clear-search');
    const liveFilter = document.getElementById('live-filter');
    const activeFilter = document.getElementById('active-filter');
    const sortBy = document.getElementById('sort-by');
    const sortOrder = document.getElementById('sort-order');
    const clearFilters = document.getElementById('clear-filters');

    if (usersSearch) {
        usersSearch.addEventListener('input', (e) => {
            clearTimeout(this.searchTimeout);
            this.searchTimeout = setTimeout(() => {
                this.currentUsersPage = 1;
                this.savePreferences({ usersSearch: e.target.value });
                this.loadUsers();
            }, 300);

            // Show/hide clear button based on input value
            if (clearSearchBtn) {
                if (e.target.value.trim()) {
                    clearSearchBtn.style.opacity = '1';
                    clearSearchBtn.style.visibility = 'visible';
                } else {
                    clearSearchBtn.style.opacity = '0';
                    clearSearchBtn.style.visibility = 'hidden';
                }
            }
        });
    }

    if (clearSearchBtn) {
        clearSearchBtn.addEventListener('click', () => {
            if (usersSearch) {
                usersSearch.value = '';
                this.currentUsersPage = 1;
                this.savePreferences({ usersSearch: '' });
                this.loadUsers();
            }
        });
    }

    // Check initial state of search input (when loaded from preferences)
    if (usersSearch && clearSearchBtn && usersSearch.value.trim()) {
        clearSearchBtn.style.opacity = '1';
        clearSearchBtn.style.visibility = 'visible';
    }

    [liveFilter, activeFilter, sortBy, sortOrder].forEach(element => {
        if (element) {
            element.addEventListener('change', (e) => {
                this.currentUsersPage = 1;

                // Save preference based on element
                if (element.id === 'live-filter') {
                    this.savePreferences({ usersLiveFilter: e.target.value });
                } else if (element.id === 'active-filter') {
                    this.savePreferences({ usersActiveFilter: e.target.value });
                } else if (element.id === 'sort-by') {
                    this.savePreferences({ usersSortBy: e.target.value });
                } else if (element.id === 'sort-order') {
                    this.savePreferences({ usersSortOrder: e.target.value });
                }

                this.loadUsers();
            });
        }
    });

    if (clearFilters) {
        clearFilters.addEventListener('click', () => this.clearFilters());
    }

    const usersTableBody = document.getElementById('users-table-body');
    if (usersTableBody) {
        usersTableBody.addEventListener('click', (event) => {
            const button = event.target.closest('button[data-user-action]');
            if (!button || !usersTableBody.contains(button)) return;

            const user = this.usersData[Number(button.dataset.userIndex)];
            if (!user || typeof user.username !== 'string') return;

            switch (button.dataset.userAction) {
                case 'check':
                    this.forceCheckUser(user.username, button);
                    break;
                case 'edit':
                    this.editUser(user.username);
                    break;
                case 'delete':
                    this.confirmDeleteUser(user.username);
                    break;
            }
        });
    }

    document.querySelectorAll('#page-numbers, #page-numbers-top').forEach(container => {
        container.addEventListener('click', (event) => {
            const button = event.target.closest('button[data-page-number]');
            if (!button) return;
            const page = Number(button.dataset.pageNumber);
            if (Number.isInteger(page) && page > 0) this.goToPage(page);
        });
    });

    // Pagination controls
    this.setupUsersPaginationListeners();
};

TikTokRecorderApp.prototype.setupUsersPaginationListeners = function() {
    // Pagination - Bottom controls
    const prevPage = document.getElementById('prev-page');
    const nextPage = document.getElementById('next-page');

    if (prevPage) {
        prevPage.addEventListener('click', () => {
            if (this.currentUsersPage > 1) {
                this.currentUsersPage--;
                this.loadUsers();
            }
        });
    }

    if (nextPage) {
        nextPage.addEventListener('click', () => {
            if (this.pagination.has_next) {
                this.currentUsersPage++;
                this.loadUsers();
            }
        });
    }

    // Pagination - Top controls
    const prevPageTop = document.getElementById('prev-page-top');
    const nextPageTop = document.getElementById('next-page-top');

    if (prevPageTop) {
        prevPageTop.addEventListener('click', () => {
            if (this.currentUsersPage > 1) {
                this.currentUsersPage--;
                this.loadUsers();
            }
        });
    }

    if (nextPageTop) {
        nextPageTop.addEventListener('click', () => {
            if (this.pagination.has_next) {
                this.currentUsersPage++;
                this.loadUsers();
            }
        });
    }
};

// Quick add user functionality
TikTokRecorderApp.prototype.quickAddUser = async function() {
    // Check read-only mode
    if (this.isReadOnlyOperation()) {
        return;
    }

    const input = document.getElementById('quick-add-username');
    const messageDiv = document.getElementById('quick-add-message');
    const submitButton = document.getElementById('quick-add-btn');
    if (!input || submitButton?.dataset.pending === 'true') return;

    const username = input.value;

    if (!username) {
        this.showMessage(messageDiv, 'error', 'Please enter a username');
        return;
    }

    const normalizedUsername = username.startsWith('@') ? username.slice(1) : username;
    if (
        !/^[A-Za-z0-9._]{2,24}$/.test(normalizedUsername)
        || normalizedUsername.endsWith('.')
    ) {
        this.showMessage(
            messageDiv,
            'error',
            'Use 2–24 letters, digits, underscores, or periods; the username cannot end with a period.'
        );
        input.focus();
        return;
    }

    try {
        if (submitButton) {
            submitButton.dataset.pending = 'true';
            submitButton.disabled = true;
            submitButton.setAttribute('aria-busy', 'true');
            submitButton.textContent = 'Adding…';
        }

        const result = await this.apiRequest('/api/users', {
            method: 'POST',
            body: JSON.stringify({ username })
        });

        this.showMessage(messageDiv, 'success', result.message);
        input.value = '';
        const clearButton = document.getElementById('clear-quick-add');
        if (clearButton) {
            clearButton.style.opacity = '0';
            clearButton.style.visibility = 'hidden';
        }

        // Refresh dashboard
        setTimeout(() => this.loadDashboard(), 1000);

    } catch (error) {
        this.showMessage(messageDiv, 'error', error.message);
    } finally {
        if (submitButton) {
            delete submitButton.dataset.pending;
            submitButton.removeAttribute('aria-busy');
            submitButton.textContent = 'Add User';
            submitButton.disabled = this.readOnlyMode;
        }
    }
};

TikTokRecorderApp.prototype.showMessage = function(container, type, message) {
    if (!container) return;

    container.className = `message ${type}`;
    container.textContent = message;
    container.classList.remove('hidden');

    clearTimeout(container._hideMessageTimer);
    container._hideMessageTimer = setTimeout(() => {
        container.classList.add('hidden');
    }, 5000);
};

// Users page methods
TikTokRecorderApp.prototype.loadUsers = async function() {
    if (this.currentPage !== 'users') return;

    const requestId = (this.usersRequestId || 0) + 1;
    this.usersRequestId = requestId;
    const tbody = document.getElementById('users-table-body');
    if (tbody) tbody.setAttribute('aria-busy', 'true');

    try {
        const usersSearch = document.getElementById('users-search');
        const liveFilter = document.getElementById('live-filter');
        const activeFilter = document.getElementById('active-filter');
        const sortBy = document.getElementById('sort-by');
        const sortOrder = document.getElementById('sort-order');

        const params = new URLSearchParams({
            page: this.currentUsersPage,
            per_page: this.usersPerPage,
            search: usersSearch ? usersSearch.value : '',
            live_filter: liveFilter ? liveFilter.value : 'all',
            active_filter: activeFilter ? activeFilter.value : 'active',
            sort_by: sortBy ? sortBy.value : 'username',
            sort_order: sortOrder ? sortOrder.value : 'asc'
        });

        const data = await this.apiRequest(`/api/users?${params}`);
        if (requestId !== this.usersRequestId) return;
        if (!data || !Array.isArray(data.users) || !data.pagination) {
            throw new Error('The server returned incomplete user data. Try again.');
        }
        this.usersData = data.users;
        this.pagination = data.pagination;

        this.renderUsersTable();
        this.updatePagination();

    } catch (error) {
        if (requestId !== this.usersRequestId) return;
        console.error('Failed to load users:', error);
        this.renderUsersError(error.message);
        this.showToast('error', 'Users unavailable', error.message);
    } finally {
        if (requestId === this.usersRequestId && tbody) {
            tbody.setAttribute('aria-busy', 'false');
        }
    }
};

TikTokRecorderApp.prototype.renderUsersError = function(message) {
    const tbody = document.getElementById('users-table-body');
    if (!tbody) return;

    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 6;
    cell.className = 'table-error-state';

    const text = document.createElement('span');
    text.textContent = message || 'Users could not be loaded.';

    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn btn-secondary btn-sm';
    retry.dataset.screenshotId = 'users-retry-load';
    retry.textContent = 'Try again';
    retry.addEventListener('click', () => this.loadUsers());

    cell.append(text, retry);
    row.appendChild(cell);
    tbody.replaceChildren(row);
};

TikTokRecorderApp.prototype.renderUsersTable = function() {
    const tbody = document.getElementById('users-table-body');
    if (!tbody) return;
    const columnCount = this.legacySchedulerStatsEnabled ? 6 : 4;

    if (this.usersData.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="${columnCount}" class="text-center" style="padding: 2rem; color: var(--text-secondary);">
                    No users found
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = this.usersData.map((user, index) => `
        <tr>
            <td data-label="User">
                <div class="user-name">${this.escapeHtml(user.username)}</div>
                ${user.live_duration ? `<div class="user-live-meta">Live: ${this.escapeHtml(user.live_duration)}</div>` : ''}
            </td>
            <td data-label="Status">
                <div class="user-status-icons">
                    <span class="status-icon ${user.is_live ? 'status-live' : 'status-offline'}" title="${user.is_live ? 'Live' : 'Offline'}" aria-label="${user.is_live ? 'Live' : 'Offline'}">
                        ${this.icon('circle')}
                    </span>
                    <span class="status-icon ${user.is_active ? 'status-active' : 'status-inactive'}" title="${user.is_active ? 'Active' : 'Inactive'}" aria-label="${user.is_active ? 'Active' : 'Inactive'}">
                        ${this.icon(user.is_active ? 'circle-check' : 'circle-x')}
                    </span>
                    ${user.is_favorite ? `<span class="status-icon status-favorite" title="Favorite" aria-label="Favorite">${this.icon('star')}</span>` : ''}
                    ${user.notifications_enabled ? `<span class="status-icon status-notifications" title="Notifications enabled" aria-label="Notifications enabled">${this.icon('bell')}</span>` : ''}
                </div>
            </td>
            ${this.legacySchedulerStatsEnabled ? `<td data-label="Interval">${this.escapeHtml(user.check_interval)}s</td>` : ''}
            <td data-label="Lives">${this.escapeHtml(user.total_lives)}</td>
            ${this.legacySchedulerStatsEnabled ? `<td data-label="Next">${this.formatNextCheck(user.next_check, user.is_active)}</td>` : ''}
            <td data-label="Actions">
                <div class="user-actions">
                    <button class="btn btn-sm btn-primary"
                            data-screenshot-id="user-check-live"
                            type="button"
                            data-user-action="check"
                            data-user-index="${index}"
                            data-read-only-disable>
                        Check Live
                    </button>
                    <button class="btn btn-sm btn-secondary"
                            data-screenshot-id="user-edit"
                            type="button"
                            data-user-action="edit"
                            data-user-index="${index}">
                        Edit
                    </button>
                    <button class="btn btn-sm btn-danger"
                            data-screenshot-id="user-delete"
                            type="button"
                            data-user-action="delete"
                            data-user-index="${index}"
                            data-read-only-disable>
                        Delete
                    </button>
                </div>
            </td>
        </tr>
    `).join('');

    // Apply read-only mode after rendering
    this.applyReadOnlyMode();
};

TikTokRecorderApp.prototype.formatNextCheck = function(nextCheck, isActive = true) {
    if (!nextCheck) return 'Not scheduled';

    // If user is inactive, show "-" instead of calculating time
    if (!isActive) {
        return '-';
    }

    try {
        // SQLite stores times in local time, parse directly
        const nextTime = new Date(nextCheck);
        const now = new Date();
        const nextTimestamp = nextTime.getTime();
        if (Number.isNaN(nextTimestamp)) return 'Invalid time';
        const diffMs = nextTimestamp - now.getTime();

        if (diffMs <= 0) {
            // Show negative delay with color-coded intensity
            const delaySeconds = Math.abs(Math.floor(diffMs / 1000));
            let colorIntensity;
            let delayText;

            if (delaySeconds < 60) {
                // < 1min: light red
                colorIntensity = '#ff9999';
                delayText = `-${delaySeconds}s`;
            } else if (delaySeconds < 300) {
                // 1-5min: medium red
                colorIntensity = '#ff6666';
                const minutes = Math.floor(delaySeconds / 60);
                const seconds = delaySeconds % 60;
                delayText = `-${minutes}m ${seconds}s`;
            } else {
                // 5min+: dark red
                colorIntensity = '#ff3333';
                const minutes = Math.floor(delaySeconds / 60);
                const hours = Math.floor(minutes / 60);
                const remainingMinutes = minutes % 60;
                if (hours > 0) {
                    delayText = `-${hours}h ${remainingMinutes}m`;
                } else {
                    delayText = `-${minutes}m`;
                }
            }

            return `<span style="color: ${colorIntensity}; font-weight: bold;">${delayText}</span>`;
        }

        const diffSeconds = Math.floor(diffMs / 1000);

        if (diffSeconds < 60) {
            return `${diffSeconds}s`;
        } else if (diffSeconds < 3600) {
            const minutes = Math.floor(diffSeconds / 60);
            const seconds = diffSeconds % 60;
            return `${minutes}m ${seconds}s`;
        } else {
            const hours = Math.floor(diffSeconds / 3600);
            const minutes = Math.floor((diffSeconds % 3600) / 60);
            return `${hours}h ${minutes}m`;
        }
    } catch (error) {
        return 'Invalid time';
    }
};

TikTokRecorderApp.prototype.updatePagination = function() {
    // Bottom pagination elements
    const info = document.getElementById('pagination-info');
    const prevBtn = document.getElementById('prev-page');
    const nextBtn = document.getElementById('next-page');
    const pageNumbers = document.getElementById('page-numbers');

    // Top pagination elements
    const infoTop = document.getElementById('pagination-info-top');
    const prevBtnTop = document.getElementById('prev-page-top');
    const nextBtnTop = document.getElementById('next-page-top');
    const pageNumbersTop = document.getElementById('page-numbers-top');

    // Update info
    const totalUsers = Number(this.pagination.total_users) || 0;
    const start = totalUsers === 0 ? 0 : (this.pagination.page - 1) * this.pagination.per_page + 1;
    const end = totalUsers === 0 ? 0 : Math.min(start + this.pagination.per_page - 1, totalUsers);
    const infoText = totalUsers === 0 ? 'Showing 0 users' : `Showing ${start}-${end} of ${totalUsers} users`;

    if (info) info.textContent = infoText;
    if (infoTop) infoTop.textContent = infoText;

    // Update buttons
    const hasNext = this.pagination.has_next;
    const hasPrev = this.pagination.has_prev;

    if (prevBtn) prevBtn.disabled = !hasPrev;
    if (nextBtn) nextBtn.disabled = !hasNext;
    if (prevBtnTop) prevBtnTop.disabled = !hasPrev;
    if (nextBtnTop) nextBtnTop.disabled = !hasNext;

    // Update page numbers
    const pages = this.generatePageNumbers();
    const pageNumbersHTML = pages.map(page => {
        if (page === '...') {
            return '<span style="padding: 0.5rem;">...</span>';
        }
        return `
            <button class="page-number ${page === this.pagination.page ? 'active' : ''}"
                    data-screenshot-id="users-page-number"
                    type="button"
                    data-page-number="${page}"
                    aria-label="Go to page ${page}"
                    ${page === this.pagination.page ? 'aria-current="page"' : ''}>
                ${page}
            </button>
        `;
    }).join('');

    if (pageNumbers) pageNumbers.innerHTML = pageNumbersHTML;
    if (pageNumbersTop) pageNumbersTop.innerHTML = pageNumbersHTML;
};

TikTokRecorderApp.prototype.generatePageNumbers = function() {
    const current = this.pagination.page;
    const total = this.pagination.total_pages;
    const pages = [];

    if (total <= 7) {
        for (let i = 1; i <= total; i++) {
            pages.push(i);
        }
    } else {
        pages.push(1);

        if (current > 4) {
            pages.push('...');
        }

        for (let i = Math.max(2, current - 2); i <= Math.min(total - 1, current + 2); i++) {
            pages.push(i);
        }

        if (current < total - 3) {
            pages.push('...');
        }

        pages.push(total);
    }

    return pages;
};

TikTokRecorderApp.prototype.goToPage = function(page) {
    this.currentUsersPage = page;
    this.loadUsers();
};

TikTokRecorderApp.prototype.clearFilters = function() {
    const usersSearch = document.getElementById('users-search');
    const liveFilter = document.getElementById('live-filter');
    const activeFilter = document.getElementById('active-filter');
    const sortBy = document.getElementById('sort-by');
    const sortOrder = document.getElementById('sort-order');

    if (usersSearch) usersSearch.value = '';
    if (liveFilter) liveFilter.value = 'all';
    if (activeFilter) activeFilter.value = 'active';
    if (sortBy) sortBy.value = 'username';
    if (sortOrder) sortOrder.value = 'asc';
    this.currentUsersPage = 1;

    // Save cleared preferences
    this.savePreferences({
        usersSearch: '',
        usersLiveFilter: 'all',
        usersActiveFilter: 'active',
        usersSortBy: 'username',
        usersSortOrder: 'asc'
    });

    this.loadUsers();
};

// User management methods
TikTokRecorderApp.prototype.editUser = function(username) {
    // Fetch latest user data directly from server
    this.fetchUserForEdit(username);
};

TikTokRecorderApp.prototype.fetchUserForEdit = async function(username) {
    try {
        const data = await this.apiRequest(`/api/users/${encodeURIComponent(username)}`);
        const user = data.user;

        // Populate modal with fresh data
        document.getElementById('edit-username').value = user.username;
        const checkIntervalInput = document.getElementById('edit-check-interval');
        if (checkIntervalInput) checkIntervalInput.value = user.check_interval;
        document.getElementById('edit-is-active').checked = user.is_active;
        document.getElementById('edit-is-favorite').checked = user.is_favorite;
        document.getElementById('edit-notifications-enabled').checked = user.notifications_enabled;

        this.showModal('edit-user-modal');

    } catch (error) {
        this.showToast('error', 'Error', 'Failed to load user data');
    }
};

TikTokRecorderApp.prototype.saveUserChanges = async function() {
    // Check read-only mode
    if (this.isReadOnlyOperation()) {
        return;
    }

    const username = document.getElementById('edit-username').value;
    const checkIntervalInput = document.getElementById('edit-check-interval');
    const checkInterval = checkIntervalInput ? parseInt(checkIntervalInput.value) : null;
    const isActive = document.getElementById('edit-is-active').checked;
    const isFavorite = document.getElementById('edit-is-favorite').checked;
    const notificationsEnabled = document.getElementById('edit-notifications-enabled').checked;
    const saveButton = document.getElementById('edit-save-btn');

    if (saveButton?.dataset.pending === 'true') return;

    // Validate check interval
    if (checkIntervalInput && (isNaN(checkInterval) || checkInterval < 1 || checkInterval > 3600)) {
        this.showToast('error', 'Invalid Input', 'Check interval must be between 1 and 3600 seconds');
        return;
    }

    const updates = {
        is_active: isActive,
        is_favorite: isFavorite,
        notifications_enabled: notificationsEnabled
    };
    if (checkIntervalInput) updates.check_interval = checkInterval;

    try {
        if (saveButton) {
            saveButton.dataset.pending = 'true';
            saveButton.disabled = true;
            saveButton.setAttribute('aria-busy', 'true');
            saveButton.textContent = 'Saving…';
        }

        await this.apiRequest(`/api/users/${encodeURIComponent(username)}`, {
            method: 'PUT',
            body: JSON.stringify(updates)
        });

        this.hideModal('edit-user-modal');
        this.showToast('success', 'Updated', `User ${username} updated successfully`);

        // Refresh current page to show changes
        if (this.currentPage === 'users') {
            this.loadUsers();
        } else if (this.currentPage === 'favorites') {
            this.loadFavoritesPage();
        }

    } catch (error) {
        this.showToast('error', 'Update failed', error.message);
    } finally {
        if (saveButton) {
            delete saveButton.dataset.pending;
            saveButton.removeAttribute('aria-busy');
            saveButton.textContent = 'Save Changes';
            saveButton.disabled = this.readOnlyMode;
        }
    }
};

TikTokRecorderApp.prototype.forceCheckUser = async function(username, triggerButton = null) {
    // Check read-only mode
    if (this.isReadOnlyOperation()) {
        return;
    }

    if (triggerButton?.dataset.pending === 'true') return;

    console.log(`🔍 Force check button clicked for user: ${username}`);
    try {
        if (triggerButton) {
            triggerButton.dataset.pending = 'true';
            triggerButton.disabled = true;
            triggerButton.setAttribute('aria-busy', 'true');
        }
        console.log(`📡 Sending force check request for: ${username}`);
        const response = await this.apiRequest(`/api/users/${encodeURIComponent(username)}/check`, {
            method: 'POST'
        });

        console.log(`📥 Force check response for ${username}:`, response);
        if (response.success) {
            this.showToast('success', 'Check Scheduled', `Immediate live check scheduled for ${username}`);
            // Refresh users table to show updated next_check time
            this.loadUsers();
        } else {
            this.showToast('error', 'Error', response.message || 'Failed to schedule check');
        }

    } catch (error) {
        console.error(`💥 Force check error for ${username}:`, error);
        this.showToast('error', 'Check failed', error.message);
    } finally {
        if (triggerButton) {
            delete triggerButton.dataset.pending;
            triggerButton.removeAttribute('aria-busy');
            triggerButton.disabled = this.readOnlyMode;
        }
    }
};

TikTokRecorderApp.prototype.confirmDeleteUser = function(username) {
    document.getElementById('confirm-title').textContent = 'Delete User';
    document.getElementById('confirm-message').innerHTML = `
        Are you sure you want to delete user <strong>${this.escapeHtml(username)}</strong>?<br>
        <small style="color: var(--text-secondary);">This will also delete all recorded live sessions and the user directory.</small>
    `;

    const confirmBtn = document.getElementById('confirm-action');
    confirmBtn.dataset.action = 'delete';
    confirmBtn.dataset.username = username;
    confirmBtn.textContent = 'Delete User';

    this.showModal('confirm-modal');
};

TikTokRecorderApp.prototype.deleteUser = async function(username) {
    // Check read-only mode
    if (this.isReadOnlyOperation()) {
        return;
    }

    const confirmButton = document.getElementById('confirm-action');
    if (confirmButton?.dataset.pending === 'true') return;

    try {
        if (confirmButton) {
            confirmButton.dataset.pending = 'true';
            confirmButton.disabled = true;
            confirmButton.setAttribute('aria-busy', 'true');
            confirmButton.textContent = 'Deleting…';
        }

        await this.apiRequest(`/api/users/${encodeURIComponent(username)}`, {
            method: 'DELETE'
        });

        this.hideModal('confirm-modal');
        this.showToast('success', 'Deleted', `User ${username} deleted successfully`);
        this.loadUsers();

    } catch (error) {
        this.showToast('error', 'Delete failed', error.message);
    } finally {
        if (confirmButton) {
            delete confirmButton.dataset.pending;
            confirmButton.removeAttribute('aria-busy');
            confirmButton.textContent = 'Delete User';
            confirmButton.disabled = this.readOnlyMode;
        }
    }
};

// Favorites and notifications methods
TikTokRecorderApp.prototype.toggleFavorite = async function(username, isFavorite) {
    // Check read-only mode
    if (this.isReadOnlyOperation()) {
        return;
    }

    try {
        await this.apiRequest(`/api/users/${encodeURIComponent(username)}/favorite`, {
            method: 'PUT',
            body: JSON.stringify({ is_favorite: isFavorite })
        });

        const action = isFavorite ? 'added to' : 'removed from';
        this.showToast('success', 'Updated', `${username} ${action} favorites`);

        // Refresh current page
        if (this.currentPage === 'users') {
            this.loadUsers();
        } else if (this.currentPage === 'favorites') {
            this.loadFavoritesPage();
        }

    } catch (error) {
        this.showToast('error', 'Error', 'Failed to update favorite status');
    }
};

TikTokRecorderApp.prototype.toggleNotifications = async function(username, notificationsEnabled) {
    // Check read-only mode
    if (this.isReadOnlyOperation()) {
        return;
    }

    try {
        await this.apiRequest(`/api/users/${encodeURIComponent(username)}/notifications`, {
            method: 'PUT',
            body: JSON.stringify({ notifications_enabled: notificationsEnabled })
        });

        const action = notificationsEnabled ? 'enabled' : 'disabled';
        this.showToast('success', 'Updated', `Notifications ${action} for ${username}`);

        // Refresh current page
        if (this.currentPage === 'users') {
            this.loadUsers();
        } else if (this.currentPage === 'favorites') {
            this.loadFavoritesPage();
        }

    } catch (error) {
        this.showToast('error', 'Error', 'Failed to update notification status');
    }
};
