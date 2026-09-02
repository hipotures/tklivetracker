/**
 * Dashboard page functionality
 */

// Dashboard methods for TikTokRecorderApp
TikTokRecorderApp.prototype.loadDashboard = async function() {
    const requestId = (this.dashboardRequestId || 0) + 1;
    this.dashboardRequestId = requestId;
    const liveContainer = document.getElementById('live-users-list');
    if (liveContainer) liveContainer.setAttribute('aria-busy', 'true');

    try {
        const response = await this.apiRequest('/api/dashboard');
        if (requestId !== this.dashboardRequestId) return;
        if (!response || !response.statistics || !Array.isArray(response.live_users)) {
            throw new Error('The server returned incomplete dashboard data. Try again.');
        }
        // The response is direct data, not wrapped in success/data
        this.updateDashboardStats(response.statistics);

        // Check for new live users before updating
        this.checkForNewLiveUsers(response.live_users);

        this.updateLiveUsers(response.live_users);
        this.updateLastRefreshTime();
    } catch (error) {
        if (requestId !== this.dashboardRequestId) return;
        console.error('Dashboard load error:', error);
        this.showToast('error', 'Dashboard unavailable', error.message);
    } finally {
        if (requestId === this.dashboardRequestId && liveContainer) {
            liveContainer.setAttribute('aria-busy', 'false');
        }
    }
};

TikTokRecorderApp.prototype.updateDashboardStats = function(stats) {
    document.getElementById('total-users').textContent = stats.total_users || 0;
    document.getElementById('live-users-count').textContent = stats.live_users || 0;
    document.getElementById('active-users').textContent = stats.active_users || 0;
    document.getElementById('total-lives').textContent = stats.total_recorded_lives || 0;
    document.getElementById('long-streams').textContent = stats.long_streams || 0;
};

TikTokRecorderApp.prototype.checkForNewLiveUsers = function(currentLiveUsers) {
    // Skip on first load
    if (!this.previousLiveUsers) {
        this.previousLiveUsers = currentLiveUsers.map(u => u.username);
        return;
    }

    const currentUsernames = currentLiveUsers.map(u => u.username);
    const newLiveUsers = currentUsernames.filter(username =>
        !this.previousLiveUsers.includes(username)
    );

    // Send notifications for new live users
    newLiveUsers.forEach(username => {
        this.sendLiveNotification(username);
    });

    // Update previous list
    this.previousLiveUsers = currentUsernames;
};

TikTokRecorderApp.prototype.sendLiveNotification = async function(username) {
    console.log('🔴 New live user detected:', username);

    if (!this.browserNotificationState || Notification.permission !== 'granted') {
        return;
    }

    // Check notification settings
    const notifyAll = this.preferences.notifyAllLive;
    let shouldNotify = notifyAll;

    if (!notifyAll) {
        // Check if this specific user has notifications enabled
        try {
            const response = await this.apiRequest(`/api/users/${encodeURIComponent(username)}`);
            shouldNotify = response.user && response.user.notifications_enabled;
        } catch (error) {
            console.error('Error checking user notification settings:', error);
            shouldNotify = false;
        }
    }

    if (shouldNotify && 'Notification' in window) {
        const title = `${username} is live!`;
        const message = `${username} started a live stream on TikTok`;

        let notification;
        try {
            notification = new Notification(title, {
                body: message,
                tag: `live-${username}`,
                requireInteraction: true
            });
        } catch (error) {
            console.warn('Browser notification could not be displayed:', error);
            return;
        }

        notification.onclick = function() {
            window.focus();
            this.close();
        };

        console.log('Live notification sent for:', username);

        // Also show toast
        this.showToast('info', 'Live Stream', `${username} started streaming!`);
    } else {
        console.log('Notification skipped for:', username, '(user notifications disabled)');
    }
};

TikTokRecorderApp.prototype.updateLiveUsers = function(liveUsers) {
    const container = document.getElementById('live-users-list');
    const badge = document.getElementById('live-badge');

    if (!container || !badge) return;

    badge.textContent = liveUsers.length;
    container.innerHTML = '';

    if (liveUsers.length === 0) {
        container.innerHTML = '<div class="no-live-users">No users currently live</div>';
        return;
    }

    liveUsers.forEach(user => {
        const userCard = document.createElement('div');
        userCard.className = 'live-user-card';

        // Format live duration and check for long streams
        let durationText = '--';
        let isLongStream = false;
        if (user.live_duration !== null && user.live_duration !== undefined) {
            const duration = Number.parseInt(user.live_duration, 10);
            const longStreamThresholdHours = 5; // Default to 5 hours

            if (!Number.isFinite(duration) || duration < 0) {
                durationText = '--';
            } else {

                // Check if this is a long stream (>5 hours)
                isLongStream = duration >= (longStreamThresholdHours * 3600);

                if (duration < 60) {
                    durationText = `${duration}s`;
                } else if (duration < 3600) {
                    const minutes = Math.floor(duration / 60);
                    const seconds = duration % 60;
                    durationText = `${minutes}m ${seconds}s`;
                } else {
                    const hours = Math.floor(duration / 3600);
                    const minutes = Math.floor((duration % 3600) / 60);
                    durationText = `${hours}h ${minutes}m`;

                    // Add warning icon for long streams
                    if (isLongStream) {
                        durationText = `${this.icon('clock', 'long-stream-icon')} ${durationText}`;
                    }
                }
            }
        }

        // Add CSS class for long streams
        if (isLongStream) {
            userCard.classList.add('long-stream');
        }

        // Favorite indicator
        const favoriteIndicator = user.is_favorite ?
            `<span class="favorite-marker" title="Favorite" aria-label="Favorite">${this.icon('star')}</span>` : '';
        const notificationIndicator = user.notifications_enabled ?
            '<span class="notification-state" title="Notifications enabled">Alerts</span>' :
            '';
        const activityStatus = user.is_active ? '' :
            '<span class="active-status inactive" title="Recording is disabled for this user">INACTIVE</span>';
        const recordingState = user.recording_state || (user.is_recording ? 'recording' : 'not_recording');
        const recordingStatus = recordingState === 'recording' ?
            '<span class="recording-status recording-active" title="A recorder process is active">RECORDING</span>' :
            recordingState === 'starting' ?
                '<span class="recording-status recording-starting" title="Recorder startup is in progress">STARTING</span>' :
                '<span class="recording-status recording-inactive" title="No recorder process is active">NOT RECORDING</span>';

        userCard.innerHTML = `
            <div class="live-user-header">
                <div class="live-user-name">
                    ${favoriteIndicator}
                    <strong>${this.escapeHtml(user.username)}</strong>
                </div>
                <div class="live-user-header-icons">
                    ${notificationIndicator}
                    ${activityStatus}
                    ${recordingStatus}
                </div>
            </div>
            <div class="live-user-info">
                <div><strong>Duration:</strong> ${durationText}</div>
                <div><strong>Started:</strong> ${this.formatTimestamp(user.started_at)}</div>
            </div>
        `;

        container.appendChild(userCard);
    });
};

TikTokRecorderApp.prototype.viewUserHistory = function(username) {
    // Navigate to users page and filter by this user
    document.getElementById('users-search').value = username;
    this.savePreferences({ usersSearch: username });
    this.navigateTo('users');
};

// Live page functionality
TikTokRecorderApp.prototype.loadLivePage = async function() {
    const requestId = (this.livePageRequestId || 0) + 1;
    this.livePageRequestId = requestId;
    const container = document.getElementById('live-page-content');
    if (container) container.setAttribute('aria-busy', 'true');

    try {
        const response = await this.apiRequest('/api/dashboard');
        if (requestId !== this.livePageRequestId) return;
        if (!response || !Array.isArray(response.live_users)) {
            throw new Error('The server returned incomplete live-stream data. Try again.');
        }
        // Use the live_users from dashboard endpoint
        this.updateLivePageContent(response.live_users);
        this.updateLastRefreshTime();
    } catch (error) {
        if (requestId !== this.livePageRequestId) return;
        console.error('Live page load error:', error);
        this.showToast('error', 'Live streams unavailable', error.message);
    } finally {
        if (requestId === this.livePageRequestId && container) {
            container.setAttribute('aria-busy', 'false');
        }
    }
};

TikTokRecorderApp.prototype.updateLivePageContent = function(liveUsers) {
    const container = document.getElementById('live-page-content');
    if (!container) return;

    container.innerHTML = '';

    if (liveUsers.length === 0) {
        container.innerHTML = '<div class="no-content">No users currently live</div>';
        return;
    }

    liveUsers.forEach(user => {
        const streamCard = document.createElement('div');
        streamCard.className = 'live-stream-card';

        // Format live duration and check for long streams
        let durationText = '--';
        let isLongStream = false;
        if (user.live_duration !== null && user.live_duration !== undefined) {
            const duration = Number.parseInt(user.live_duration, 10);
            const longStreamThresholdHours = 5; // Default to 5 hours

            if (!Number.isFinite(duration) || duration < 0) {
                durationText = '--';
            } else {

            // Check if this is a long stream (>5 hours)
                isLongStream = duration >= (longStreamThresholdHours * 3600);

                if (duration < 60) {
                durationText = `${duration}s`;
                } else if (duration < 3600) {
                const minutes = Math.floor(duration / 60);
                const seconds = duration % 60;
                durationText = `${minutes}m ${seconds}s`;
                } else {
                const hours = Math.floor(duration / 3600);
                const minutes = Math.floor((duration % 3600) / 60);
                durationText = `${hours}h ${minutes}m`;

                // Add warning icon for long streams
                    if (isLongStream) {
                        durationText = `${this.icon('clock', 'long-stream-icon')} ${durationText}`;
                    }
                }
            }
        }

        // Add CSS class for long streams
        if (isLongStream) {
            streamCard.classList.add('long-stream');
        }

        // Recording status
        const recordingState = user.recording_state || (user.is_recording ? 'recording' : 'not_recording');
        const recordingStatus = recordingState === 'recording' ?
            '<span class="recording-status recording-active">Recording</span>' :
            recordingState === 'starting' ?
                '<span class="recording-status recording-starting">Starting recorder</span>' :
                '<span class="recording-status recording-inactive">Not recording</span>';
        const checkIntervalInfo = this.legacySchedulerStatsEnabled === true ?
            `<div><strong>Check Interval:</strong> ${user.check_interval}s</div>` : '';

        // Stream quality indicator
        const qualityIndicator = user.stream_quality ?
            `<div class="stream-quality">${this.escapeHtml(user.stream_quality)}</div>` : '';

        streamCard.innerHTML = `
            <div class="stream-header">
                <div class="stream-user">
                    ${user.is_favorite ? `<span class="favorite-marker" title="Favorite" aria-label="Favorite">${this.icon('star')}</span>` : ''}
                    <strong>${this.escapeHtml(user.username)}</strong>
                </div>
                <div class="stream-header-icons">
                    ${recordingStatus}
                </div>
            </div>

            <div class="stream-info">
                <div><strong>Duration:</strong> ${durationText}</div>
                <div><strong>Current Live Started:</strong> ${this.formatTimestamp(user.started_at)}</div>
                <div><strong>Previous Live:</strong> ${this.formatPreviousLiveInfo(user.previous_live_started_at, user.previous_live_ended_at)}</div>
                ${checkIntervalInfo}
                <div><strong>Total Lives:</strong> ${user.total_lives || 0}</div>
            </div>

            ${qualityIndicator}
        `;

        container.appendChild(streamCard);
    });
};

// Favorites page functionality
TikTokRecorderApp.prototype.loadFavoritesPage = async function() {
    const requestId = (this.favoritesRequestId || 0) + 1;
    this.favoritesRequestId = requestId;
    const container = document.getElementById('favorites-content');
    if (container) container.setAttribute('aria-busy', 'true');

    try {
        const response = await this.apiRequest('/api/favorites');
        if (requestId !== this.favoritesRequestId) return;
        if (!response || !Array.isArray(response.favorites)) {
            throw new Error('The server returned incomplete favorites data. Try again.');
        }
        // API returns direct data: { favorites: [...], count: 5 }
        const summary = {
            total_favorites: response.count,
            favorites_live: response.favorites.filter(f => f.is_live).length
        };
        this.updateFavoritesSummary(summary);
        this.updateFavoritesContent(response.favorites);
        this.updateLastRefreshTime();
    } catch (error) {
        if (requestId !== this.favoritesRequestId) return;
        console.error('Favorites page load error:', error);
        this.showToast('error', 'Favorites unavailable', error.message);
    } finally {
        if (requestId === this.favoritesRequestId && container) {
            container.setAttribute('aria-busy', 'false');
        }
    }
};

TikTokRecorderApp.prototype.updateFavoritesSummary = function(summary) {
    document.getElementById('favorites-total').textContent = summary.total_favorites || 0;
    document.getElementById('favorites-live').textContent = summary.favorites_live || 0;
};

TikTokRecorderApp.prototype.updateFavoritesContent = function(favorites) {
    const container = document.getElementById('favorites-content');
    if (!container) return;

    container.innerHTML = '';

    if (favorites.length === 0) {
        container.innerHTML = '<div class="no-content">No favorite users yet. Star users to add them to favorites!</div>';
        return;
    }

    favorites.forEach(user => {
        const favoriteCard = document.createElement('div');
        favoriteCard.className = 'favorite-card';

        // Live status
        const liveStatus = user.is_live ?
            '<div class="live-status live">LIVE</div>' :
            '<div class="live-status offline">Offline</div>';

        // Active status
        const activeStatus = user.is_active ?
            '<span class="active-status active" title="Active">Active</span>' :
            '<span class="active-status inactive" title="Inactive">Inactive</span>';

        // Next check countdown
        let nextCheckDisplay = '--';
        if (user.next_check) {
            const nextCheck = new Date(user.next_check);
            const now = new Date();
            const diffSeconds = Math.max(0, Math.floor((nextCheck - now) / 1000));

            if (diffSeconds === 0) {
                nextCheckDisplay = 'Now';
            } else if (diffSeconds < 60) {
                nextCheckDisplay = `${diffSeconds}s`;
            } else if (diffSeconds < 3600) {
                const minutes = Math.floor(diffSeconds / 60);
                const seconds = diffSeconds % 60;
                nextCheckDisplay = `${minutes}m ${seconds}s`;
            } else {
                const hours = Math.floor(diffSeconds / 3600);
                const minutes = Math.floor((diffSeconds % 3600) / 60);
                nextCheckDisplay = `${hours}h ${minutes}m`;
            }
        }
        const lastLiveDaysAgo = Number.isInteger(user.last_live_days_ago) ? user.last_live_days_ago : -1;

        favoriteCard.innerHTML = `
            <div class="favorite-header">
                <div class="favorite-user">
                    <strong>${this.escapeHtml(user.username)}</strong>
                    ${activeStatus}
                    ${liveStatus}
                </div>
            </div>

            <div class="favorite-info">
                <div><strong>Last Live:</strong> ${lastLiveDaysAgo}</div>
                <div><strong>Check Interval:</strong> ${user.check_interval}s</div>
                <div><strong>Total Lives:</strong> ${user.total_lives || 0}</div>
                <div><strong>Next Check:</strong> ${nextCheckDisplay}</div>
                <div><strong>Notifications:</strong> <span class="notification-value ${user.notifications_enabled ? 'notifications-on' : ''}" title="Notifications ${user.notifications_enabled ? 'enabled' : 'disabled'}">${this.icon(user.notifications_enabled ? 'bell' : 'bell-off')}<span class="visually-hidden">${user.notifications_enabled ? 'Enabled' : 'Disabled'}</span></span></div>
            </div>
        `;

        container.appendChild(favoriteCard);
    });
};

// Auto-refresh functionality
TikTokRecorderApp.prototype.startAutoRefresh = function() {
    // Initialize progress tracking
    this.refreshStartTime = Date.now();
    this.refreshInterval = 60000; // 1 minute

    // Full refresh every 1 minute
    this.mainRefreshInterval = setInterval(() => {
        this.refreshStartTime = Date.now();
        this.updateRefreshProgress(100); // Reset to 100%

        if (this.currentPage === 'dashboard') {
            this.loadDashboard();
        } else if (this.currentPage === 'live') {
            this.loadLivePage();
        } else if (this.currentPage === 'favorites') {
            this.loadFavoritesPage();
        } else if (this.currentPage === 'users') {
            console.log('🔄 Auto-refreshing users data from database...');
            this.loadUsers();
        } else if (this.currentPage === 'stats') {
            console.log('📊 Auto-refreshing stats data...');
            this.loadStats();
        } else if (this.currentPage === 'admin') {
            console.log('⚙️ Auto-refreshing admin data...');
            this.loadAdminPage();
        }

        // Always check supervisor status
        this.checkSupervisorStatus();

        // Update "Last updated" timestamp on all pages
        this.updateLastRefreshTime();
    }, 60000); // 1 minute

    // Update progress indicator every 1 second (countdown from 100% to 0%)
    this.progressInterval = setInterval(() => {
        const elapsed = Date.now() - this.refreshStartTime;
        const remaining = Math.max(0, this.refreshInterval - elapsed);
        const progress = (remaining / this.refreshInterval) * 100;
        this.updateRefreshProgress(progress);
    }, 1000); // Every 1 second

    // Initialize progress to 100%
    this.updateRefreshProgress(100);

    // Set initial timestamp
    this.updateLastRefreshTime();
};

TikTokRecorderApp.prototype.stopAutoRefresh = function() {
    if (this.mainRefreshInterval) {
        clearInterval(this.mainRefreshInterval);
        this.mainRefreshInterval = null;
    }
    if (this.progressInterval) {
        clearInterval(this.progressInterval);
        this.progressInterval = null;
    }
};

TikTokRecorderApp.prototype.refreshCurrentPage = function() {
    switch (this.currentPage) {
        case 'dashboard':
            this.loadDashboard();
            break;
        case 'users':
            this.loadUsers();
            break;
        case 'live':
            this.loadLivePage();
            break;
        case 'favorites':
            this.loadFavoritesPage();
            break;
        case 'stats':
            this.loadStats();
            break;
        case 'analytics':
            this.loadCurrentAnalyticsTab();
            break;
    }
};

TikTokRecorderApp.prototype.forceMainRefresh = function() {
    // Reset refresh timer and progress
    this.refreshStartTime = Date.now();
    this.updateRefreshProgress(100); // Reset to 100%

    this.refreshCurrentPage();
    this.updateLastRefreshTime();

    // Visual feedback
    const refreshIcon = document.getElementById('main-refresh-icon');
    if (refreshIcon) {
        refreshIcon.style.animation = 'spin 0.5s linear';
        setTimeout(() => {
            refreshIcon.style.animation = '';
        }, 500);
    }
};

TikTokRecorderApp.prototype.updateLastRefreshTime = function() {
    const element = document.getElementById('last-updated');
    if (element) {
        const now = new Date();
        element.textContent = `Last updated: ${now.toLocaleTimeString('en-GB')}`;
    }
};

TikTokRecorderApp.prototype.updateRefreshProgress = function(percentage) {
    const progressElement = document.getElementById('refresh-progress');
    if (progressElement) {
        progressElement.style.width = `${percentage}%`;
    }
};

// Server-Sent Events for real-time updates
TikTokRecorderApp.prototype.setupServerSentEvents = function() {
    // Check if SSE endpoint exists first
    try {
        if (this.eventSource) {
            this.eventSource.close();
        }

        this.eventSource = new EventSource('/api/events');

        this.eventSource.onopen = () => {
            this.updateSSEStatus(true);
        };

        this.eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.handleServerEvent(data);
            } catch (error) {
                console.error('Error parsing server event:', error);
            }
        };

        this.eventSource.onerror = () => {
            this.updateSSEStatus(false);
            // Don't try to reconnect automatically to avoid 404 spam
            console.warn('SSE connection failed - real-time updates disabled');
        };
    } catch (error) {
        console.warn('SSE not available:', error);
        this.updateSSEStatus(false);
    }
};

TikTokRecorderApp.prototype.handleServerEvent = function(data) {
    switch (data.type) {
        case 'user_live_status_changed':
            if (this.currentPage === 'dashboard' || this.currentPage === 'live' || this.currentPage === 'favorites') {
                this.refreshCurrentPage();
            }

            // Show notification for live status changes
            if (data.is_live && data.notifications_enabled) {
                this.showLiveNotification(data.username);
            }
            break;

        case 'user_added':
        case 'user_updated':
        case 'user_deleted':
            if (this.currentPage === 'users' || this.currentPage === 'dashboard') {
                this.refreshCurrentPage();
            }
            break;

        case 'recording_started':
        case 'recording_stopped':
            if (this.currentPage === 'dashboard' || this.currentPage === 'live') {
                this.refreshCurrentPage();
            }
            break;

        case 'recorder_room_id_unavailable':
            this.showToast(
                'warning',
                'Recording did not start',
                data.message || `${data.username} is LIVE, but TikTok did not provide RoomID.`
            );
            break;
    }
};

TikTokRecorderApp.prototype.updateSSEStatus = function(connected) {
    const statusElement = document.getElementById('sse-status');
    if (statusElement) {
        const dot = statusElement.querySelector('.dot');
        const accessibleText = statusElement.querySelector('.visually-hidden');
        const statusText = connected ? 'Live updates: connected' : 'Live updates: disconnected';
        if (connected) {
            dot.style.backgroundColor = '#10b981'; // Green
            statusElement.title = statusText;
        } else {
            dot.style.backgroundColor = '#ef4444'; // Red
            statusElement.title = statusText;
        }
        if (accessibleText) accessibleText.textContent = statusText;
    }
};

TikTokRecorderApp.prototype.updateSupervisorStatus = function(status, message, minutesAgo) {
    const statusElement = document.getElementById('supervisor-status');
    if (statusElement) {
        const dot = statusElement.querySelector('.dot');
        const accessibleText = statusElement.querySelector('.visually-hidden');
        let statusText;

        if (status === 'connected') {
            dot.style.backgroundColor = '#10b981'; // Green
            statusText = `Supervisor: Active (${message})`;
        } else if (status === 'warning') {
            dot.style.backgroundColor = '#f59e0b'; // Yellow
            statusText = `Supervisor: Warning (${message})`;
        } else {
            dot.style.backgroundColor = '#ef4444'; // Red
            statusText = `Supervisor: Disconnected (${message})`;
        }
        statusElement.title = statusText;
        if (accessibleText) accessibleText.textContent = statusText;
    }
};

TikTokRecorderApp.prototype.showLiveNotification = function(username) {
    // Browser notification
    if ('Notification' in window && Notification.permission === 'granted') {
        try {
            new Notification(`${username} is now live!`, {
                body: 'Click to view live streams',
                tag: `live-${username}`
            });
        } catch (error) {
            console.warn('Browser notification could not be displayed:', error);
        }
    }

    // In-app notification
    this.showToast('info', 'User Live', `${username} is now streaming!`);
};

// Helper functions for formatting timestamps
TikTokRecorderApp.prototype.formatTimestamp = function(timestamp) {
    if (!timestamp) return '--';

    try {
        const date = new Date(timestamp);
        if (Number.isNaN(date.getTime())) return '--';
        const now = new Date();
        const diffMs = now - date;
        const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
        const diffMinutes = Math.floor(diffMs / (1000 * 60));

        // If less than 1 hour ago, show relative time
        if (diffMs >= 0 && diffHours < 1) {
            if (diffMinutes < 1) {
                return 'Just now';
            }
            return `${diffMinutes}m ago`;
        }

        // If today, show time only (24-hour format)
        if (date.toDateString() === now.toDateString()) {
            return date.toLocaleTimeString('en-GB', {
                hour: '2-digit',
                minute: '2-digit',
                hour12: false
            });
        }

        // If yesterday, show "Yesterday + time" (24-hour format)
        const yesterday = new Date(now);
        yesterday.setDate(yesterday.getDate() - 1);
        if (date.toDateString() === yesterday.toDateString()) {
            return `Yesterday ${date.toLocaleTimeString('en-GB', {
                hour: '2-digit',
                minute: '2-digit',
                hour12: false
            })}`;
        }

        // Otherwise show full date and time (24-hour format)
        return date.toLocaleString('en-GB', {
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false
        });
    } catch (error) {
        console.error('Error formatting timestamp:', error);
        return '--';
    }
};

TikTokRecorderApp.prototype.formatPreviousLiveInfo = function(startedAt, endedAt) {
    if (!endedAt) return 'No previous live';

    try {
        const endDate = new Date(endedAt);
        if (Number.isNaN(endDate.getTime())) return 'Unknown';
        const now = new Date();
        const diffMs = now - endDate;
        const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
        const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

        let timeAgo;
        if (diffDays > 0) {
            timeAgo = `${diffDays}d ago`;
        } else if (diffHours > 0) {
            timeAgo = `${diffHours}h ago`;
        } else {
            const diffMinutes = Math.floor(diffMs / (1000 * 60));
            if (diffMinutes > 0) {
                timeAgo = `${diffMinutes}m ago`;
            } else {
                timeAgo = 'Just ended';
            }
        }

        return timeAgo;
    } catch (error) {
        console.error('Error formatting previous live info:', error);
        return 'Unknown';
    }
};

// Supervisor Status Functions
TikTokRecorderApp.prototype.checkSupervisorStatus = async function() {
    try {
        const response = await this.apiRequest('/api/supervisor-status');
        this.updateSupervisorStatus(response.status, response.message, response.last_seen_minutes);
    } catch (error) {
        console.error('Failed to check supervisor status:', error);
        this.updateSupervisorStatus('error', 'Failed to check status', 0);
    }
};

TikTokRecorderApp.prototype.loadAdminPage = async function() {
    if (this.currentPage !== 'admin') return;

    try {
        const response = await this.apiRequest('/api/supervisor-details');
        this.renderAdminPage(response);
    } catch (error) {
        console.error('Failed to load admin page:', error);
        this.showToast('error', 'Error', 'Failed to load admin data');
    }
};

TikTokRecorderApp.prototype.loadDatabaseStatistics = async function() {
    if (this.currentPage !== 'admin') return;

    try {
        const response = await this.apiRequest('/api/database-statistics');
        const database = response.database;
        const formatCount = value => Number(value || 0).toLocaleString('en-US');
        const formatSize = bytes => {
            const value = Number(bytes || 0);
            if (value < 1024) return `${value} B`;
            const units = ['KB', 'MB', 'GB', 'TB'];
            let size = value;
            let unit = -1;
            do {
                size /= 1024;
                unit += 1;
            } while (size >= 1024 && unit < units.length - 1);
            return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[unit]}`;
        };
        const values = {
            'db-size': formatSize(database.size_bytes),
            'db-table-count': formatCount(database.table_count),
            'db-user-count': formatCount(database.users),
            'db-live-count': formatCount(database.lives),
            'db-process-count': formatCount(database.process_records),
            'db-active-recorder-count': formatCount(database.active_recorders)
        };

        Object.entries(values).forEach(([id, value]) => {
            const element = document.getElementById(id);
            if (element) element.textContent = value;
        });
    } catch (error) {
        console.error('Failed to load database statistics:', error);
        this.showToast('error', 'Error', 'Failed to load database statistics');
    }
};

TikTokRecorderApp.prototype.renderAdminPage = function(data) {
    const supervisor = data.supervisor;
    const stats = data.stats;
    const system = data.system;

    // Update supervisor status indicator
    const adminIndicator = document.getElementById('admin-supervisor-indicator');
    if (adminIndicator) {
        const dot = adminIndicator.querySelector('.dot');
        const text = document.getElementById('admin-supervisor-status-text');

        if (supervisor.status === 'connected') {
            dot.style.backgroundColor = '#10b981'; // Green
            if (text) text.textContent = 'Active';
        } else if (supervisor.status === 'warning') {
            dot.style.backgroundColor = '#f59e0b'; // Yellow
            if (text) text.textContent = 'Warning';
        } else {
            dot.style.backgroundColor = '#ef4444'; // Red
            if (text) text.textContent = 'Disconnected';
        }
    }

    // Update supervisor details
    const details = {
        'supervisor-pid': supervisor.pid,
        'supervisor-started': supervisor.started_at,
        'supervisor-heartbeat': supervisor.last_heartbeat,
        'supervisor-version': supervisor.version,
        'supervisor-config-hash': supervisor.config_hash,
        'supervisor-uptime': supervisor.uptime
    };

    Object.entries(details).forEach(([id, value]) => {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    });

    // Update statistics
    const statsMap = {
        'admin-live-detections': stats.live_detections || 0,
        'admin-recording-starts': stats.recording_starts || 0,
        'admin-recording-stops': stats.recording_stops || 0,
        'admin-health-checks': stats.health_checks || 0,
        'admin-restarts': stats.restarts || 0
    };

    Object.entries(statsMap).forEach(([id, value]) => {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    });

    // Update system information
    const systemMap = {
        'db-connection-status': system.database.connection,
        'db-path': system.database.path,
        'recordings-path': system.config.recordings_path,
        'log-path': system.config.log_path,
        'check-new-users': system.config.check_new_users,
        'default-check-interval': system.config.default_check_interval,
        'max-check-interval': system.config.max_check_interval,
        'after-live-check-interval': system.config.after_live_check_interval,
        'supervisor-heartbeat-interval': system.config.supervisor_heartbeat_interval,
        'health-check-interval': system.config.health_check_interval,
        'max-live-processes': system.config.max_live_processes,
        'detached-process-mode': system.config.detached_process_mode
    };

    Object.entries(systemMap).forEach(([id, value]) => {
        const element = document.getElementById(id);
        if (element) {
            // For boolean values, display as Yes/No
            if (typeof value === 'boolean') {
                element.textContent = value ? 'Yes' : 'No';
            } else if (id.includes('interval') && typeof value === 'number') {
                // For time intervals, add 's' suffix
                element.textContent = value + 's';
            } else {
                element.textContent = value;
            }
        }
    });
};
