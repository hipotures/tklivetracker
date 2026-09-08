/**
 * User preferences and theme management functionality
 */

// Theme Management Methods
TikTokRecorderApp.prototype.initializeTheme = function() {
    // Apply saved theme on load
    if (this.currentTheme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
};

TikTokRecorderApp.prototype.toggleTheme = function() {
    // Toggle between light and dark
    this.currentTheme = this.currentTheme === 'light' ? 'dark' : 'light';

    // Apply theme to DOM
    if (this.currentTheme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }

    // Save preference to localStorage
    try {
        localStorage.setItem('theme', this.currentTheme);
    } catch (error) {
        console.warn('Theme preference could not be saved', error);
    }

    // Update button title
    const themeToggle = document.getElementById('theme-toggle');
    if (themeToggle) {
        themeToggle.title = this.currentTheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    }
};

TikTokRecorderApp.prototype.getTheme = function() {
    return this.currentTheme;
};

// User Preferences Management
TikTokRecorderApp.prototype.loadPreferences = function() {
    const stored = this.getStorageItem('userPreferences');
    const defaults = {
        // Page state
        currentPage: 'dashboard',

        // Users page
        usersPerPage: 50,
        usersSearch: '',
        usersLiveFilter: 'all',
        usersActiveFilter: 'active',
        usersSortBy: 'username',
        usersSortOrder: 'asc',

        // Stats page
        statsActiveOnly: true,
        statsSortBy: 'next_check',
        statsSortOrder: 'asc',

        // Analytics page
        analyticsView: 'month',
        currentAnalyticsTab: 'live-sessions',

        // Notification settings
        notifyAllLive: false,

        // Version for future migrations
        version: '1.0'
    };

    if (stored) {
        try {
            const parsed = JSON.parse(stored);
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                throw new TypeError('Preferences must be an object');
            }
            // Merge with defaults to handle missing keys
            const preferences = { ...defaults, ...parsed };
            const allowedPages = ['dashboard', 'users', 'live', 'favorites', 'stats', 'analytics', 'admin'];
            const allowedLiveFilters = ['all', 'live', 'offline'];
            const allowedActiveFilters = [
                'all', 'active', 'inactive', 'notifications_enabled', 'notifications_disabled'
            ];
            const allowedUserSorts = [
                'username', 'live', 'active', 'total_lives'
            ];
            if (this.legacySchedulerStatsEnabled) allowedUserSorts.push('interval', 'next_check');
            const legacyAnalyticsViews = {
                hourly: 'day',
                last24h: 'day',
                '7d': 'week',
                last7d: 'week',
                daily: 'month',
                '30d': 'month',
                last30d: 'month',
                weekly: 'quarter',
                '3m': 'quarter',
                '12m': 'year',
                last365d: 'year'
            };
            preferences.analyticsView =
                legacyAnalyticsViews[preferences.analyticsView] || preferences.analyticsView;
            const allowedAnalyticsViews = [
                'day', 'week', 'month', 'quarter', 'year', 'all'
            ];

            if (!allowedPages.includes(preferences.currentPage)) preferences.currentPage = defaults.currentPage;
            if (!allowedLiveFilters.includes(preferences.usersLiveFilter)) preferences.usersLiveFilter = defaults.usersLiveFilter;
            if (!allowedActiveFilters.includes(preferences.usersActiveFilter)) preferences.usersActiveFilter = defaults.usersActiveFilter;
            if (!allowedUserSorts.includes(preferences.usersSortBy)) preferences.usersSortBy = defaults.usersSortBy;
            if (!['asc', 'desc'].includes(preferences.usersSortOrder)) preferences.usersSortOrder = defaults.usersSortOrder;
            if (!allowedAnalyticsViews.includes(preferences.analyticsView)) preferences.analyticsView = defaults.analyticsView;
            if (!['live-sessions', 'new-users'].includes(preferences.currentAnalyticsTab)) {
                preferences.currentAnalyticsTab = defaults.currentAnalyticsTab;
            }
            if (!Number.isInteger(preferences.usersPerPage) || preferences.usersPerPage < 1 || preferences.usersPerPage > 100) {
                preferences.usersPerPage = defaults.usersPerPage;
            }
            preferences.usersSearch = String(preferences.usersSearch || '').slice(0, 255);

            return preferences;
        } catch (e) {
            console.warn('Failed to parse preferences, using defaults', e);
            return defaults;
        }
    }

    return defaults;
};

TikTokRecorderApp.prototype.savePreferences = function(updates = {}) {
    this.preferences = { ...this.preferences, ...updates };
    try {
        localStorage.setItem('userPreferences', JSON.stringify(this.preferences));
    } catch (e) {
        console.error('Failed to save preferences', e);
    }
};

TikTokRecorderApp.prototype.applyPreferences = function() {
    // Apply users page preferences
    if (this.currentPage === 'users') {
        const usersSearch = document.getElementById('users-search');
        const liveFilter = document.getElementById('live-filter');
        const activeFilter = document.getElementById('active-filter');
        const sortBy = document.getElementById('sort-by');
        const sortOrder = document.getElementById('sort-order');

        if (usersSearch) usersSearch.value = this.preferences.usersSearch || '';
        if (liveFilter) liveFilter.value = this.preferences.usersLiveFilter || 'all';
        if (activeFilter) activeFilter.value = this.preferences.usersActiveFilter || 'active';
        if (sortBy) sortBy.value = this.preferences.usersSortBy || 'username';
        if (sortOrder) sortOrder.value = this.preferences.usersSortOrder || 'asc';
        this.usersPerPage = this.preferences.usersPerPage || 50;
    }

    // Apply stats page preferences
    if (this.currentPage === 'stats') {
        const activeOnlyFilter = document.getElementById('active-only-filter');
        const statsSortBy = document.getElementById('stats-sort-by');
        const statsSortOrder = document.getElementById('stats-sort-order');

        if (activeOnlyFilter) {
            activeOnlyFilter.checked = this.preferences.statsActiveOnly !== false;
        }
        if (statsSortBy) statsSortBy.value = this.preferences.statsSortBy || 'next_check';
        if (statsSortOrder) statsSortOrder.value = this.preferences.statsSortOrder || 'asc';
    }

    // Apply analytics preferences
    if (this.currentPage === 'analytics') {
        this.analyticsView = this.preferences.analyticsView || 'month';
        const analyticsView = document.getElementById('analytics-view');
        if (analyticsView) analyticsView.value = this.analyticsView;
    }

    // Apply notification preferences (admin page)
    if (this.currentPage === 'admin') {
        const notifyAllLive = document.getElementById('notify-all-live');
        if (notifyAllLive) {
            notifyAllLive.checked = this.preferences.notifyAllLive || false;
        }
    }
};

TikTokRecorderApp.prototype.resetPreferences = function() {
    if (confirm('Reset all preferences to defaults? This will not affect your theme setting.')) {
        try {
            localStorage.removeItem('userPreferences');
        } catch (error) {
            console.warn('Saved preferences could not be removed', error);
        }
        this.preferences = this.loadPreferences();
        this.applyPreferences();
        this.showToast('success', 'Settings Reset', 'Preferences reset to defaults');

        // Reload current page
        this.navigateTo(this.currentPage);
    }
};

// Local browser notifications are available only while the dashboard is open.
TikTokRecorderApp.prototype.initializeBrowserNotifications = function() {
    this.browserNotificationState.permission = this.browserNotificationState.supported
        ? Notification.permission
        : 'unavailable';
};

TikTokRecorderApp.prototype.requestNotificationPermission = async function() {
    if (!this.browserNotificationState.supported) {
        this.showToast('error', 'Not Supported', 'Browser notifications are not supported');
        return false;
    }

    if (Notification.permission === 'granted') {
        return true;
    }

    try {
        const permission = await Notification.requestPermission();
        this.browserNotificationState.permission = permission;

        if (permission === 'granted') {
            return true;
        } else if (permission === 'denied') {
            this.showToast('warning', 'Permission Denied', 'Enable browser notifications in browser settings');
            return false;
        } else {
            this.showToast('info', 'Permission Required', 'Allow browser notifications to receive live alerts');
            return false;
        }
    } catch (error) {
        console.error('Notification permission request failed:', error);
        this.showToast('error', 'Permission Error', 'Failed to request browser notification permission');
        return false;
    }
};


TikTokRecorderApp.prototype.testBrowserNotification = async function() {
    if (!(await this.requestNotificationPermission())) return;

    try {
        const notification = new Notification('TkLiveTracker', {
            body: 'Browser notifications are working.',
            tag: 'test-notification'
        });
        notification.onclick = function() {
            window.focus();
            this.close();
        };
        this.showToast('success', 'Test Sent', 'Browser notification displayed');
    } catch (error) {
        console.error('Failed to show notification:', error);
        this.showToast('error', 'Test Failed', 'Failed to display browser notification');
    }
};
