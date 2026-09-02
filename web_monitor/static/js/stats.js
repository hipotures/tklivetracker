/**
 * Statistics page functionality
 */

// Stats page event listeners
TikTokRecorderApp.prototype.setupStatsEventListeners = function() {
    // Active only filter
    const activeOnlyFilter = document.getElementById('active-only-filter');
    if (activeOnlyFilter) {
        activeOnlyFilter.addEventListener('change', (e) => {
            this.savePreferences({ statsActiveOnly: e.target.checked });
            this.loadStats();
        });
    }

    // Sort controls
    const delaysLimit = document.getElementById('delays-limit');
    const statsSortBy = document.getElementById('stats-sort-by');
    const statsSortOrder = document.getElementById('stats-sort-order');

    if (delaysLimit) {
        delaysLimit.addEventListener('change', (e) => {
            this.savePreferences({ delaysLimit: e.target.value });
            this.loadStats();
        });
    }
    if (statsSortBy) {
        statsSortBy.addEventListener('change', (e) => {
            this.savePreferences({ statsSortBy: e.target.value });
            this.loadStats();
        });
    }
    if (statsSortOrder) {
        statsSortOrder.addEventListener('change', (e) => {
            this.savePreferences({ statsSortOrder: e.target.value });
            this.loadStats();
        });
    }

    // Interval users sort controls
    const intervalUsersSortBy = document.getElementById('interval-users-sort-by');
    const intervalUsersSortOrder = document.getElementById('interval-users-sort-order');
    const closeIntervalUsers = document.getElementById('close-interval-users');

    if (intervalUsersSortBy) {
        intervalUsersSortBy.addEventListener('change', () => this.loadIntervalUsers());
    }
    if (intervalUsersSortOrder) {
        intervalUsersSortOrder.addEventListener('change', () => this.loadIntervalUsers());
    }
    if (closeIntervalUsers) {
        closeIntervalUsers.addEventListener('click', () => this.hideIntervalUsers());
    }

    // New users controls
    const newUsersLimit = document.getElementById('new-users-limit');
    const newUsersSortBy = document.getElementById('new-users-sort-by');
    const newUsersSortOrder = document.getElementById('new-users-sort-order');

    if (newUsersLimit) {
        newUsersLimit.addEventListener('change', () => this.loadNewUsers());
    }
    if (newUsersSortBy) {
        newUsersSortBy.addEventListener('change', () => this.loadNewUsers());
    }
    if (newUsersSortOrder) {
        newUsersSortOrder.addEventListener('change', () => this.loadNewUsers());
    }

    // Recent live users controls
    const recentLiveUsersLimit = document.getElementById('recent-live-users-limit');
    const recentLiveUsersSortBy = document.getElementById('recent-live-users-sort-by');
    const recentLiveUsersSortOrder = document.getElementById('recent-live-users-sort-order');

    if (recentLiveUsersLimit) {
        recentLiveUsersLimit.addEventListener('change', () => this.loadRecentLiveUsers());
    }
    if (recentLiveUsersSortBy) {
        recentLiveUsersSortBy.addEventListener('change', () => this.loadRecentLiveUsers());
    }
    if (recentLiveUsersSortOrder) {
        recentLiveUsersSortOrder.addEventListener('change', () => this.loadRecentLiveUsers());
    }

    // Stats table header sorting
    const self = this;
    document.addEventListener('click', (e) => {
        if (e.target.closest('.sortable-header')) {
            const header = e.target.closest('.sortable-header');
            const sortField = header.dataset.sort;
            const table = header.closest('table');
            const tableContainer = header.closest('.stats-table-container');
            const section = tableContainer?.parentElement;

            if (section?.classList.contains('stats-users-delays')) {
                self.handleStatsTableSort(sortField);
            } else if (section?.classList.contains('stats-interval-users')) {
                self.handleIntervalUsersTableSort(sortField);
            } else if (section?.classList.contains('stats-new-users')) {
                self.handleNewUsersTableSort(sortField);
            } else if (section?.classList.contains('stats-recent-live-users')) {
                self.handleRecentLiveUsersTableSort(sortField);
            }
        }
    });
};

// Stats page methods
TikTokRecorderApp.prototype.loadStats = async function() {
    try {
        const activeOnlyFilter = document.getElementById('active-only-filter');
        const delaysLimit = document.getElementById('delays-limit');
        const statsSortBy = document.getElementById('stats-sort-by');
        const statsSortOrder = document.getElementById('stats-sort-order');

        const activeOnly = activeOnlyFilter ? activeOnlyFilter.checked : true;
        const limit = delaysLimit ? delaysLimit.value : 10;
        const sortBy = statsSortBy ? statsSortBy.value : 'next_check';
        const sortOrder = statsSortOrder ? statsSortOrder.value : 'asc';

        const params = new URLSearchParams({
            active_only: activeOnly ? 'true' : 'false',
            limit: limit,
            sort_by: sortBy,
            sort_order: sortOrder
        });

        const data = await this.apiRequest(`/api/stats?${params}`);
        this.renderStats(data);
        this.updateStatsTableSortIndicators(sortBy, sortOrder);
    } catch (error) {
        console.error('Error loading stats:', error);
        this.showToast('error', 'Error', 'Failed to load statistics');
    }
};

TikTokRecorderApp.prototype.renderStats = function(data) {
    // Update system overview
    const systemStats = data.system_stats;
    const totalUsersEl = document.getElementById('stats-total-users');
    const activeUsersEl = document.getElementById('stats-active-users');
    const liveUsersEl = document.getElementById('stats-live-users');
    const favoritesEl = document.getElementById('stats-favorites');
    const cycleTimeEl = document.getElementById('stats-cycle-time');

    if (totalUsersEl) totalUsersEl.textContent = systemStats.total_users || '--';
    if (activeUsersEl) activeUsersEl.textContent = systemStats.active_users || '--';

    // Update live users with capacity format (e.g., "8/20")
    if (liveUsersEl) {
        if (data.live_processes_capacity) {
            const capacity = data.live_processes_capacity;
            const capacityText = `${capacity.active_processes}/${capacity.max_processes}`;
            liveUsersEl.textContent = capacityText;
        } else {
            liveUsersEl.textContent = systemStats.live_users || '--';
        }
    }

    if (favoritesEl) favoritesEl.textContent = systemStats.favorites_count || '--';
    if (cycleTimeEl) cycleTimeEl.textContent = systemStats.estimated_cycle_time || '--';

    // Update delay statistics
    const delayStats = data.delay_stats;
    const delayNoDelayEl = document.getElementById('delay-no-delay-count');
    const delay0To60El = document.getElementById('delay-0-60-count');
    const delay60To120El = document.getElementById('delay-60-120-count');
    const delay120To180El = document.getElementById('delay-120-180-count');
    const delay180PlusEl = document.getElementById('delay-180-plus-count');

    if (delayNoDelayEl) delayNoDelayEl.textContent = delayStats.no_delay || 0;
    if (delay0To60El) delay0To60El.textContent = delayStats.delay_0_60 || 0;
    if (delay60To120El) delay60To120El.textContent = delayStats.delay_60_120 || 0;
    if (delay120To180El) delay120To180El.textContent = delayStats.delay_120_180 || 0;
    if (delay180PlusEl) delay180PlusEl.textContent = delayStats.delay_180_plus || 0;

    // Update interval info
    const avgIntervalEl = document.getElementById('stats-avg-interval');
    const minIntervalEl = document.getElementById('stats-min-interval');
    const maxIntervalEl = document.getElementById('stats-max-interval');

    if (avgIntervalEl) avgIntervalEl.textContent = `${systemStats.avg_interval}s`;
    if (minIntervalEl) minIntervalEl.textContent = `${systemStats.min_interval}s`;
    if (maxIntervalEl) maxIntervalEl.textContent = `${systemStats.max_interval}s`;

    // Render users with delays table
    this.renderDelayTable(data.users_with_delays);

    // Render interval distribution
    this.renderIntervalDistribution(data.interval_distribution);

    // Load and render new users table
    this.loadNewUsers();

    // Load and render recent live users table
    this.loadRecentLiveUsers();
};

TikTokRecorderApp.prototype.renderDelayTable = function(users) {
    const tbody = document.getElementById('stats-delays-table-body');
    if (!tbody) return;

    tbody.innerHTML = '';

    if (!users || users.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No users found</td></tr>';
        return;
    }

    users.forEach(user => {
        const row = document.createElement('tr');
        row.className = `delay-row ${user.delay_category}`;

        const delayText = user.delay_seconds === 0 ? '0s' : `${user.delay_seconds}s`;
        const statusText = this.getUserStatusForStats(user);
        const nextCheckText = user.next_check ?
            new Date(user.next_check).toLocaleString(undefined, {
                day: '2-digit',
                month: '2-digit',
                hour: '2-digit',
                minute: '2-digit'
            }) : 'NULL';

        row.innerHTML = `
            <td>
                <a href="#" class="username-link" data-screenshot-id="stats-open-user" data-username="${this.escapeHtml(user.username)}">
                    ${this.escapeHtml(user.username)}
                </a>
            </td>
            <td>
                <span class="delay-cell ${user.delay_category}">${delayText}</span>
            </td>
            <td>${user.check_interval}s</td>
            <td>${nextCheckText}</td>
            <td>${statusText}</td>
        `;

        tbody.appendChild(row);
    });

    // Add click handlers for username links
    tbody.querySelectorAll('.username-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const username = e.target.dataset.username;
            this.navigateToUserInUsersPage(username);
        });
    });
};

TikTokRecorderApp.prototype.renderIntervalDistribution = function(intervals) {
    const container = document.getElementById('intervals-distribution');
    if (!container) return;

    container.innerHTML = '';

    if (!intervals || intervals.length === 0) {
        container.innerHTML = '<div style="text-align: center; color: var(--text-muted);">No interval data available</div>';
        return;
    }

    // Sort intervals by value (ascending)
    const sortedIntervals = [...intervals].sort((a, b) => a.interval - b.interval);

    sortedIntervals.forEach(item => {
        const div = document.createElement('div');
        div.className = 'interval-item clickable';
        div.dataset.screenshotId = 'stats-interval';
        div.style.cursor = 'pointer';
        div.title = `Click to view ${item.count} users with ${this.formatInterval(item.interval)} interval`;

        const intervalText = this.formatInterval(item.interval);

        div.innerHTML = `
            <div class="interval-value">${intervalText}</div>
            <div class="interval-label">Check Interval</div>
            <div class="interval-count">${item.count} users</div>
        `;

        // Add click handler
        div.addEventListener('click', () => this.showIntervalUsers(item.interval));

        container.appendChild(div);
    });
};

TikTokRecorderApp.prototype.formatInterval = function(seconds) {
    if (seconds >= 3600) {
        const hours = Math.floor(seconds / 3600);
        const mins = Math.floor((seconds % 3600) / 60);
        return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
    } else if (seconds >= 60) {
        const mins = Math.floor(seconds / 60);
        const secs = seconds % 60;
        return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
    } else {
        return `${seconds}s`;
    }
};

TikTokRecorderApp.prototype.getUserStatusForStats = function(user) {
    // Unified status icons for all tables
    const statusIcons = [];

    // Base statuses - always show
    statusIcons.push(`<span class="status-icon ${user.is_live ? 'status-live' : 'status-offline'}" title="${user.is_live ? 'Live' : 'Offline'}" aria-label="${user.is_live ? 'Live' : 'Offline'}">${this.icon('circle')}</span>`);
    statusIcons.push(`<span class="status-icon ${user.is_active ? 'status-active' : 'status-inactive'}" title="${user.is_active ? 'Active' : 'Inactive'}" aria-label="${user.is_active ? 'Active' : 'Inactive'}">${this.icon(user.is_active ? 'circle-check' : 'circle-x')}</span>`);

    // Additional statuses - only if true
    if (user.is_favorite) {
        statusIcons.push(`<span class="status-icon status-favorite" title="Favorite" aria-label="Favorite">${this.icon('star')}</span>`);
    }
    if (user.notifications_enabled) {
        statusIcons.push(`<span class="status-icon status-notifications" title="Notifications enabled" aria-label="Notifications enabled">${this.icon('bell')}</span>`);
    }

    return '<div style="display: flex; gap: 0.5rem; align-items: center;">' + statusIcons.join('') + '</div>';
};

TikTokRecorderApp.prototype.handleStatsTableSort = function(sortField) {
    const sortBySelect = document.getElementById('stats-sort-by');
    const sortOrderSelect = document.getElementById('stats-sort-order');

    if (!sortBySelect || !sortOrderSelect) return;

    // If clicking on the same field, toggle order
    if (sortBySelect.value === sortField) {
        sortOrderSelect.value = sortOrderSelect.value === 'asc' ? 'desc' : 'asc';
    } else {
        // New field, set to ascending by default
        sortBySelect.value = sortField;
        sortOrderSelect.value = 'asc';
    }

    this.loadStats();
};

TikTokRecorderApp.prototype.updateStatsTableSortIndicators = function(sortBy, sortOrder) {
    // Reset all indicators in delays table only
    const delaysTable = document.querySelector('.stats-users-delays');
    if (delaysTable) {
        delaysTable.querySelectorAll('.sort-indicator').forEach(indicator => {
            indicator.classList.remove('active');
            indicator.textContent = '↕';
        });

        // Set active indicator in delays table
        const activeHeader = delaysTable.querySelector(`[data-sort="${sortBy}"] .sort-indicator`);
        if (activeHeader) {
            activeHeader.classList.add('active');
            activeHeader.textContent = sortOrder === 'asc' ? '↑' : '↓';
        }
    }
};

TikTokRecorderApp.prototype.navigateToUserInUsersPage = function(username) {
    // Navigate to Users page
    this.navigateTo('users');

    // Wait for page to load, then search for the user
    setTimeout(() => {
        const searchInput = document.getElementById('users-search');
        if (searchInput) {
            searchInput.value = username;
            // Trigger search
            searchInput.dispatchEvent(new Event('input'));

            // Show success message
            this.showToast('success', 'Navigation', `Navigated to Users page and searched for: ${username}`);
        }
    }, 100);
};

// Interval Users Methods
TikTokRecorderApp.prototype.showIntervalUsers = function(interval) {
    this.currentInterval = interval;
    this.loadIntervalUsers();

    // Show the table section
    const section = document.getElementById('stats-interval-users');
    if (section) {
        section.style.display = 'block';
    }
};

TikTokRecorderApp.prototype.hideIntervalUsers = function() {
    const section = document.getElementById('stats-interval-users');
    if (section) {
        section.style.display = 'none';
    }
    this.currentInterval = null;
};

TikTokRecorderApp.prototype.loadIntervalUsers = async function() {
    if (!this.currentInterval) return;

    try {
        const activeOnlyFilter = document.getElementById('active-only-filter');
        const intervalUsersSortBy = document.getElementById('interval-users-sort-by');
        const intervalUsersSortOrder = document.getElementById('interval-users-sort-order');

        const activeOnly = activeOnlyFilter ? activeOnlyFilter.checked : true;
        const sortBy = intervalUsersSortBy ? intervalUsersSortBy.value : 'username';
        const sortOrder = intervalUsersSortOrder ? intervalUsersSortOrder.value : 'asc';

        const params = new URLSearchParams({
            active_only: activeOnly ? 'true' : 'false',
            sort_by: sortBy,
            sort_order: sortOrder
        });

        const data = await this.apiRequest(`/api/users/by-interval/${this.currentInterval}?${params}`);
        this.renderIntervalUsersTable(data);
        this.updateIntervalUsersTableSortIndicators(sortBy, sortOrder);
    } catch (error) {
        console.error('Error loading interval users:', error);
        this.showToast('error', 'Error', 'Failed to load users for interval');
    }
};

TikTokRecorderApp.prototype.renderIntervalUsersTable = function(data) {
    // Update title
    const title = document.getElementById('interval-users-title');
    if (title) {
        const intervalText = this.formatInterval(data.interval);
        title.textContent = `Users with ${intervalText} Interval (${data.count} users)`;
    }

    const tbody = document.getElementById('interval-users-table-body');
    if (!tbody) return;

    tbody.innerHTML = '';

    if (!data.users || data.users.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No users found</td></tr>';
        return;
    }

    data.users.forEach(user => {
        const row = document.createElement('tr');

        const delayText = user.delay_seconds !== null && user.delay_seconds !== undefined ?
            `${user.delay_seconds}s` : '0s';

        const nextCheckText = user.next_check ?
            new Date(user.next_check).toLocaleString(undefined, {
                day: '2-digit',
                month: '2-digit',
                hour: '2-digit',
                minute: '2-digit'
            }) : 'NULL';

        const statusText = this.getUserStatusForStats(user);

        row.innerHTML = `
            <td>
                <a href="#" class="username-link" data-screenshot-id="stats-open-user" data-username="${this.escapeHtml(user.username)}">
                    ${this.escapeHtml(user.username)}
                </a>
            </td>
            <td>${delayText}</td>
            <td>${nextCheckText}</td>
            <td>${statusText}</td>
        `;

        tbody.appendChild(row);
    });

    // Add click handlers for username links
    tbody.querySelectorAll('.username-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const username = e.target.dataset.username;
            this.navigateToUserInUsersPage(username);
        });
    });
};

TikTokRecorderApp.prototype.handleIntervalUsersTableSort = function(sortField) {
    const sortBySelect = document.getElementById('interval-users-sort-by');
    const sortOrderSelect = document.getElementById('interval-users-sort-order');

    if (!sortBySelect || !sortOrderSelect) return;

    // If clicking on the same field, toggle order
    if (sortBySelect.value === sortField) {
        sortOrderSelect.value = sortOrderSelect.value === 'asc' ? 'desc' : 'asc';
    } else {
        // New field, set to ascending by default
        sortBySelect.value = sortField;
        sortOrderSelect.value = 'asc';
    }

    this.loadIntervalUsers();
};

TikTokRecorderApp.prototype.updateIntervalUsersTableSortIndicators = function(sortBy, sortOrder) {
    // Reset all indicators in interval users table
    const intervalTable = document.querySelector('#stats-interval-users');
    if (intervalTable) {
        intervalTable.querySelectorAll('.sort-indicator').forEach(indicator => {
            indicator.classList.remove('active');
            indicator.textContent = '↕';
        });

        // Set active indicator
        const activeHeader = intervalTable.querySelector(`[data-sort="${sortBy}"] .sort-indicator`);
        if (activeHeader) {
            activeHeader.classList.add('active');
            activeHeader.textContent = sortOrder === 'asc' ? '↑' : '↓';
        }
    }
};

// New Users functionality
TikTokRecorderApp.prototype.loadNewUsers = async function() {
    try {
        const limitSelect = document.getElementById('new-users-limit');
        const sortBySelect = document.getElementById('new-users-sort-by');
        const sortOrderSelect = document.getElementById('new-users-sort-order');

        const limit = limitSelect ? limitSelect.value : 10;
        const sortBy = sortBySelect ? sortBySelect.value : 'added_at';
        const sortOrder = sortOrderSelect ? sortOrderSelect.value : 'desc';

        const params = new URLSearchParams({
            limit: limit,
            sort_by: sortBy,
            sort_order: sortOrder
        });

        const data = await this.apiRequest(`/api/new-users?${params}`);
        this.renderNewUsersTable(data.users);
        this.updateNewUsersTableSortIndicators(sortBy, sortOrder);
    } catch (error) {
        console.error('Error loading new users:', error);
        this.showToast('error', 'Error', 'Failed to load new users');
    }
};

TikTokRecorderApp.prototype.renderNewUsersTable = function(users) {
    const tbody = document.getElementById('new-users-table-body');
    if (!tbody) return;

    tbody.innerHTML = '';
    const showLegacySchedulerStats = this.legacySchedulerStatsEnabled === true;
    const emptyColspan = showLegacySchedulerStats ? 5 : 4;

    if (!users || users.length === 0) {
        tbody.innerHTML = `<tr><td colspan="${emptyColspan}" style="text-align: center; color: var(--text-muted);">No users found</td></tr>`;
        return;
    }

    users.forEach(user => {
        const row = document.createElement('tr');

        const addedAtText = user.added_at ?
            new Date(user.added_at).toLocaleString(undefined, {
                day: '2-digit',
                month: '2-digit',
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            }) : 'N/A';

        const statusText = this.getUserStatusForStats(user);
        const intervalCell = showLegacySchedulerStats ? `<td>${user.check_interval}s</td>` : '';

        row.innerHTML = `
            <td>
                <a href="#" class="username-link" data-screenshot-id="stats-open-user" data-username="${this.escapeHtml(user.username)}">
                    ${this.escapeHtml(user.username)}
                </a>
            </td>
            <td>${addedAtText}</td>
            ${intervalCell}
            <td>${user.total_lives || 0}</td>
            <td>${statusText}</td>
        `;

        tbody.appendChild(row);
    });

    // Add click handlers for username links
    tbody.querySelectorAll('.username-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const username = e.target.dataset.username;
            this.navigateToUserInUsersPage(username);
        });
    });
};

TikTokRecorderApp.prototype.handleNewUsersTableSort = function(sortField) {
    const sortBySelect = document.getElementById('new-users-sort-by');
    const sortOrderSelect = document.getElementById('new-users-sort-order');

    if (!sortBySelect || !sortOrderSelect) return;

    // If clicking on the same field, toggle order
    if (sortBySelect.value === sortField) {
        sortOrderSelect.value = sortOrderSelect.value === 'asc' ? 'desc' : 'asc';
    } else {
        // New field, set to descending by default (newest first for added_at)
        sortBySelect.value = sortField;
        sortOrderSelect.value = sortField === 'added_at' ? 'desc' : 'asc';
    }

    this.loadNewUsers();
};

TikTokRecorderApp.prototype.updateNewUsersTableSortIndicators = function(sortBy, sortOrder) {
    // Reset all indicators in new users table
    const newUsersTable = document.querySelector('.stats-new-users');
    if (newUsersTable) {
        newUsersTable.querySelectorAll('.sort-indicator').forEach(indicator => {
            indicator.classList.remove('active');
            indicator.textContent = '↕';
        });

        // Set active indicator
        const activeHeader = newUsersTable.querySelector(`[data-sort="${sortBy}"] .sort-indicator`);
        if (activeHeader) {
            activeHeader.classList.add('active');
            activeHeader.textContent = sortOrder === 'asc' ? '↑' : '↓';
        }
    }
};

// Recent Live Users Methods
TikTokRecorderApp.prototype.loadRecentLiveUsers = async function() {
    try {
        const limitSelect = document.getElementById('recent-live-users-limit');
        const sortBySelect = document.getElementById('recent-live-users-sort-by');
        const sortOrderSelect = document.getElementById('recent-live-users-sort-order');

        const limit = limitSelect ? limitSelect.value : '10';
        const sortBy = sortBySelect ? sortBySelect.value : 'last_live_at';
        const sortOrder = sortOrderSelect ? sortOrderSelect.value : 'desc';

        const params = new URLSearchParams({
            limit: limit,
            sort_by: sortBy,
            sort_order: sortOrder
        });

        const data = await this.apiRequest(`/api/recent-live-users?${params}`);
        this.renderRecentLiveUsersTable(data.users);

    } catch (error) {
        console.error('Error loading recent live users:', error);
        this.showToast('error', 'Error', 'Failed to load recent live users');
    }
};

TikTokRecorderApp.prototype.renderRecentLiveUsersTable = function(users) {
    const tbody = document.getElementById('recent-live-users-table-body');
    if (!tbody) return;

    tbody.innerHTML = '';

    if (!users || users.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="no-data">No recent live users found</td></tr>';
        return;
    }

    users.forEach(user => {
        const row = document.createElement('tr');

        // Format last live date
        const lastLiveDate = user.last_live_at ? new Date(user.last_live_at).toLocaleString(undefined, {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
        }) : 'Never';

        // Use unified status format - map is_currently_live to is_live for consistency
        const userForStatus = {
            ...user,
            is_live: user.is_currently_live || false
        };
        const statusText = this.getUserStatusForStats(userForStatus);

        // Currently Live status
        const currentlyLiveText = user.is_currently_live ?
            '<span class="live-indicator">Live</span>' :
            '<span class="offline-indicator">Offline</span>';

        row.innerHTML = `
            <td>
                <button class="username-link username-link-button" data-screenshot-id="stats-open-user" type="button"
                        title="View this user on the Users page">
                    ${this.escapeHtml(user.username)}
                </button>
            </td>
            <td>${lastLiveDate}</td>
            <td>${currentlyLiveText}</td>
            <td>${user.total_lives}</td>
            <td>${statusText}</td>
        `;

        const usernameButton = row.querySelector('.username-link-button');
        if (usernameButton) {
            usernameButton.addEventListener('click', () => {
                this.navigateToUserInUsersPage(user.username);
            });
        }

        tbody.appendChild(row);
    });
};

TikTokRecorderApp.prototype.handleRecentLiveUsersTableSort = function(sortField) {
    const sortBySelect = document.getElementById('recent-live-users-sort-by');
    const sortOrderSelect = document.getElementById('recent-live-users-sort-order');

    if (!sortBySelect || !sortOrderSelect) return;

    // If same field, toggle order; otherwise set to ascending
    if (sortBySelect.value === sortField) {
        sortOrderSelect.value = sortOrderSelect.value === 'asc' ? 'desc' : 'asc';
    } else {
        sortBySelect.value = sortField;
        sortOrderSelect.value = 'asc';
    }

    this.loadRecentLiveUsers();
};

TikTokRecorderApp.prototype.updateRecentLiveUsersTableSortIndicators = function(sortBy, sortOrder) {
    // Reset all indicators in recent live users table
    const recentLiveUsersTable = document.querySelector('.stats-recent-live-users');
    if (recentLiveUsersTable) {
        recentLiveUsersTable.querySelectorAll('.sort-indicator').forEach(indicator => {
            indicator.classList.remove('active');
            indicator.textContent = '↕';
        });

        // Set active indicator
        const activeHeader = recentLiveUsersTable.querySelector(`[data-sort="${sortBy}"] .sort-indicator`);
        if (activeHeader) {
            activeHeader.classList.add('active');
            activeHeader.textContent = sortOrder === 'asc' ? '↑' : '↓';
        }
    }
};
