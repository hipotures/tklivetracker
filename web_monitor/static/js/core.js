/**
 * Core TkLiveTracker App
 */

class TikTokRecorderApp {
    constructor() {
        this.currentPage = 'dashboard';
        this.currentUsersPage = 1;
        this.usersPerPage = 50;
        this.usersData = [];
        this.pagination = {};
        this.refreshInterval = null;
        this.searchTimeout = null;
        this.eventSource = null;
        this.pendingRequestCount = 0;
        this.requestTimeoutMs = 15000;
        this.toastCounter = 0;
        this.modalReturnFocus = null;
        this.supportedPages = new Set([
            'dashboard', 'users', 'live', 'favorites', 'stats', 'analytics', 'admin'
        ]);
        this.featureFlags = window.TTRACKER_CONFIG?.featureFlags || {};
        this.legacySchedulerStatsEnabled = this.featureFlags.legacySchedulerStatsEnabled === true;

        // Analytics properties
        this.analyticsChart = null;
        this.newUsersChart = null;
        this.analyticsView = 'month';
        const analyticsNow = new Date();
        const analyticsYear = analyticsNow.getFullYear();
        const analyticsMonth = String(analyticsNow.getMonth() + 1).padStart(2, '0');
        const analyticsDay = String(analyticsNow.getDate()).padStart(2, '0');
        this.analyticsDate = `${analyticsYear}-${analyticsMonth}-${analyticsDay}`;
        this.currentAnalyticsTab = 'live-sessions';

        // Theme properties
        this.currentTheme = this.getStorageItem('theme') || 'dark';

        // User preferences
        this.preferences = this.loadPreferences();
        this.analyticsView = this.preferences.analyticsView || 'month';
        this.currentAnalyticsTab = this.preferences.currentAnalyticsTab || 'live-sessions';

        // Local browser notifications while the dashboard is open
        this.browserNotificationState = {
            supported: 'Notification' in window,
            permission: 'Notification' in window ? Notification.permission : 'unavailable'
        };

        // Read-only mode state
        this.readOnlyMode = false;

        this.init();
    }

    // Initialize the application
    init() {
        this.initializeTheme();
        this.initializeBrowserNotifications();
        this.checkReadOnlyMode();
        this.setupEventListeners();
        this.setupUsersEventListeners();
        this.setupStatsEventListeners();
        this.setupAnalyticsEventListeners();
        this.setupMobileChromeAutoHide();

        if ('onLine' in navigator && navigator.onLine === false) {
            this.setConnectionStatus(
                false,
                'You appear to be offline. Check your connection, then try again.'
            );
        }

        // Navigate to last visited page or dashboard
        const lastPage = this.preferences.currentPage || 'dashboard';
        this.navigateTo(lastPage);

        this.startAutoRefresh();
        this.setupServerSentEvents();

        // Initial supervisor status check
        setTimeout(() => this.checkSupervisorStatus(), 1000);

        window.addEventListener('beforeunload', () => {
            this.stopAutoRefresh();
            if (this.eventSource) this.eventSource.close();
        }, { once: true });
    }

    // Setup all event listeners
    setupEventListeners() {
        // Navigation
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', (e) => {
                const page = e.currentTarget.dataset.page;
                this.navigateTo(page);
            });
        });

        // Quick add user
        const quickAddForm = document.getElementById('quick-add-form');
        const quickAddInput = document.getElementById('quick-add-username');
        const clearQuickAddBtn = document.getElementById('clear-quick-add');

        if (quickAddForm) {
            quickAddForm.addEventListener('submit', (event) => {
                event.preventDefault();
                this.quickAddUser();
            });
        }
        if (quickAddInput) {
            // Show/hide clear button based on input value
            quickAddInput.addEventListener('input', (e) => {
                if (clearQuickAddBtn) {
                    if (e.target.value.trim()) {
                        clearQuickAddBtn.style.opacity = '1';
                        clearQuickAddBtn.style.visibility = 'visible';
                    } else {
                        clearQuickAddBtn.style.opacity = '0';
                        clearQuickAddBtn.style.visibility = 'hidden';
                    }
                }
            });

            // Check initial state
            if (clearQuickAddBtn && quickAddInput.value.trim()) {
                clearQuickAddBtn.style.opacity = '1';
                clearQuickAddBtn.style.visibility = 'visible';
            }
        }

        if (clearQuickAddBtn) {
            clearQuickAddBtn.addEventListener('click', () => {
                if (quickAddInput) {
                    quickAddInput.value = '';
                    clearQuickAddBtn.style.opacity = '0';
                    clearQuickAddBtn.style.visibility = 'hidden';
                    quickAddInput.focus();
                }
            });
        }

        // Theme toggle
        const themeToggle = document.getElementById('theme-toggle');
        if (themeToggle) {
            themeToggle.addEventListener('click', () => this.toggleTheme());
        }

        // Reset preferences
        const resetPreferences = document.getElementById('reset-preferences');
        if (resetPreferences) {
            resetPreferences.addEventListener('click', () => this.resetPreferences());
        }

        // Test local browser notifications
        const testBrowserNotification = document.getElementById('test-browser-notification');
        if (testBrowserNotification) {
            testBrowserNotification.addEventListener('click', () => this.testBrowserNotification());
        }

        // Notify all live checkbox
        const notifyAllLive = document.getElementById('notify-all-live');
        if (notifyAllLive) {
            notifyAllLive.addEventListener('change', (e) => {
                this.savePreferences({ notifyAllLive: e.target.checked });
                console.log('Notify all live setting:', e.target.checked);
            });
        }

        // Main refresh icon
        const mainRefreshIcon = document.getElementById('main-refresh-icon');
        if (mainRefreshIcon) {
            mainRefreshIcon.addEventListener('click', () => this.forceMainRefresh());
        }

        // Admin refresh button
        const adminRefreshBtn = document.getElementById('admin-refresh');
        if (adminRefreshBtn) {
            adminRefreshBtn.addEventListener('click', async () => {
                adminRefreshBtn.disabled = true;
                try {
                    await Promise.all([
                        this.loadAdminPage(),
                        this.loadDatabaseStatistics()
                    ]);
                } finally {
                    adminRefreshBtn.disabled = false;
                }
            });
        }

        const connectionRetry = document.getElementById('connection-retry');
        if (connectionRetry) {
            connectionRetry.addEventListener('click', () => this.forceMainRefresh());
        }

        window.addEventListener('offline', () => {
            this.setConnectionStatus(false, 'You appear to be offline. Check your connection, then try again.');
        });
        window.addEventListener('online', () => {
            this.setConnectionStatus(true);
            this.forceMainRefresh();
        });

        this.setupModalEventListeners();
    }

    setupMobileChromeAutoHide() {
        const mobileLayout = window.matchMedia('(max-width: 900px)');
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
        let scrollAnchor = Math.max(0, window.scrollY);
        let framePending = false;

        const showChrome = () => document.body.classList.remove('mobile-chrome-hidden');
        const updateChrome = () => {
            framePending = false;
            const currentScroll = Math.max(0, window.scrollY);

            if (!mobileLayout.matches || reducedMotion.matches || document.body.classList.contains('modal-open')) {
                showChrome();
                scrollAnchor = currentScroll;
                return;
            }

            if (currentScroll <= 24) {
                showChrome();
                scrollAnchor = currentScroll;
                return;
            }

            const focusedInChrome = document.activeElement?.closest?.('.header, .sidebar');
            const delta = currentScroll - scrollAnchor;
            if (delta > 12 && currentScroll > 120 && !focusedInChrome) {
                document.body.classList.add('mobile-chrome-hidden');
                scrollAnchor = currentScroll;
            } else if (delta < -12) {
                showChrome();
                scrollAnchor = currentScroll;
            }
        };

        window.addEventListener('scroll', () => {
            if (!framePending) {
                framePending = true;
                window.requestAnimationFrame(updateChrome);
            }
        }, { passive: true });
        window.addEventListener('resize', updateChrome);
        document.querySelector('.header')?.addEventListener('focusin', showChrome);
        document.querySelector('.sidebar')?.addEventListener('focusin', showChrome);
    }

    // Setup modal event listeners
    setupModalEventListeners() {
        // Close modals
        document.querySelectorAll('.modal-close, [data-dismiss="modal"]').forEach(button => {
            button.addEventListener('click', (e) => {
                const modal = e.target.closest('.modal');
                if (modal) this.hideModal(modal.id);
            });
        });

        // Edit user form
        const editForm = document.getElementById('edit-user-form');
        if (editForm) {
            editForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.saveUserChanges();
            });
        }

        // Confirm action
        const confirmBtn = document.getElementById('confirm-action');
        if (confirmBtn) {
            confirmBtn.addEventListener('click', () => {
                const action = confirmBtn.dataset.action;
                const username = confirmBtn.dataset.username;

                if (action === 'delete' && username) {
                    this.deleteUser(username);
                }
            });
        }

        // Close modals on outside click
        document.querySelectorAll('.modal').forEach(modal => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    this.hideModal(modal.id);
                }
            });
        });

        // Close modals on ESC key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.closeTopModal();
            } else if (e.key === 'Tab') {
                this.trapModalFocus(e);
            }
        });
    }

    // Check read-only mode from server
    async checkReadOnlyMode() {
        try {
            const config = await fetch('/api/config');
            const data = await config.json();
            this.readOnlyMode = data.read_only || false;
            this.applyReadOnlyMode();
        } catch (error) {
            console.error('Failed to check read-only mode:', error);
        }
    }

    // Apply read-only mode to UI elements
    applyReadOnlyMode() {
        if (this.readOnlyMode) {
            // Disable all elements with data-read-only-disable attribute
            document.querySelectorAll('[data-read-only-disable]').forEach(element => {
                element.disabled = true;
                element.title = element.title || 'Read-only mode: Operation not allowed';
            });

            // Add read-only class to body for CSS styling
            document.body.classList.add('read-only-mode');
        } else {
            // Re-enable elements if not in read-only mode
            document.querySelectorAll('[data-read-only-disable]').forEach(element => {
                element.disabled = false;
                if (element.title && element.title.includes('Read-only mode')) {
                    element.title = '';
                }
            });

            document.body.classList.remove('read-only-mode');
        }
    }

    // Check if operation is allowed in read-only mode
    isReadOnlyOperation(showMessage = true) {
        if (this.readOnlyMode && showMessage) {
            this.showToast('warning', 'Read-Only Mode', 'Write operations are not allowed in read-only mode');
            return true;
        }
        return this.readOnlyMode;
    }

    // Navigation
    navigateTo(page) {
        const safePage = this.supportedPages.has(page) ? page : 'dashboard';

        // Update navigation
        document.querySelectorAll('.nav-item').forEach(item => {
            item.classList.remove('active');
            item.removeAttribute('aria-current');
        });
        const activeNav = Array.from(document.querySelectorAll('.nav-item')).find(
            item => item.dataset.page === safePage
        );
        if (activeNav) {
            activeNav.classList.add('active');
            activeNav.setAttribute('aria-current', 'page');
        }

        // Update pages
        document.querySelectorAll('.page').forEach(p => {
            p.classList.remove('active');
            p.setAttribute('aria-hidden', 'true');
        });
        const activePage = document.getElementById(`${safePage}-page`);
        if (activePage) {
            activePage.classList.add('active');
            activePage.setAttribute('aria-hidden', 'false');
        }

        this.currentPage = safePage;
        document.body.classList.remove('mobile-chrome-hidden');
        window.scrollTo({ top: 0, behavior: 'auto' });

        // Save current page preference
        this.savePreferences({ currentPage: safePage });

        // Apply page-specific preferences
        setTimeout(() => this.applyPreferences(), 50);

        // Load page data
        switch (safePage) {
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
            case 'admin':
                this.loadAdminPage();
                break;
            case 'analytics':
                this.switchAnalyticsTab(this.currentAnalyticsTab);
                break;
        }
    }

    // API Methods
    async apiRequest(endpoint, options = {}) {
        const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        let requestTimedOut = false;
        let timeoutId = null;

        try {
            this.showLoading();

            const headers = {
                Accept: 'application/json',
                ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
                ...(options.headers || {})
            };

            if (controller) {
                timeoutId = setTimeout(() => {
                    requestTimedOut = true;
                    controller.abort();
                }, this.requestTimeoutMs);
            }

            const response = await fetch(endpoint, {
                ...options,
                headers,
                ...(controller ? { signal: controller.signal } : {})
            });
            this.setConnectionStatus(true);

            const responseText = await response.text();
            let data = null;
            if (responseText) {
                try {
                    data = JSON.parse(responseText);
                } catch (jsonError) {
                    if (response.ok) {
                        throw new Error('The server returned an invalid response. Try again.');
                    }
                }
            }

            if (!response.ok) {
                const serverMessage = data && typeof data.error === 'string' ? data.error : '';
                const error = new Error(
                    this.getApiErrorMessage(response.status, serverMessage)
                );
                error.status = response.status;
                error.data = data;
                throw error;
            }

            return data;
        } catch (error) {
            console.error('API request failed:', error);
            if (requestTimedOut || error.name === 'AbortError') {
                this.setConnectionStatus(
                    false,
                    'The server took too long to respond. Check the connection, then try again.'
                );
                throw new Error('The request timed out. Check the connection and try again.');
            }
            if (error instanceof TypeError) {
                this.setConnectionStatus(
                    false,
                    'The server could not be reached. Check your connection, then try again.'
                );
                throw new Error('Unable to reach the server. Check your connection and try again.');
            }
            throw error;
        } finally {
            if (timeoutId !== null) clearTimeout(timeoutId);
            this.hideLoading();
        }
    }

    getApiErrorMessage(status, serverMessage) {
        if (serverMessage && status < 500) return serverMessage;

        const messages = {
            400: 'The request was not valid. Check the entered values and try again.',
            401: 'Your session is no longer authorized. Reload the page and try again.',
            403: 'You do not have permission to perform this action.',
            404: 'The requested item could not be found.',
            429: 'Too many requests were sent. Wait a moment and try again.'
        };
        if (messages[status]) return messages[status];
        if (status >= 500) return 'The server encountered a problem. Try again shortly.';
        return `The request failed (status ${status}). Try again.`;
    }

    // Utility methods
    getStorageItem(key) {
        try {
            return localStorage.getItem(key);
        } catch (error) {
            console.warn(`Browser storage is unavailable for ${key}`, error);
            return null;
        }
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = String(text ?? '');
        return div.innerHTML;
    }

    icon(name, className = '') {
        return `<svg class="ui-icon ${className}" aria-hidden="true"><use href="/static/vendor/lucide/icons.svg#${name}"></use></svg>`;
    }

    showModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            this.modalReturnFocus = document.activeElement;
            modal.classList.add('show');
            modal.setAttribute('aria-hidden', 'false');
            document.body.classList.add('modal-open');
            const firstInput = modal.querySelector(
                'input:not(:disabled):not([readonly]), select:not(:disabled), textarea:not(:disabled)'
            ) || modal.querySelector('button:not(:disabled)');
            const focusTarget = firstInput || modal.querySelector('.modal-content');
            if (focusTarget) {
                if (!focusTarget.hasAttribute('tabindex') && focusTarget.classList.contains('modal-content')) {
                    focusTarget.setAttribute('tabindex', '-1');
                }
                setTimeout(() => focusTarget.focus(), 0);
            }
        }
    }

    hideModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.remove('show');
            modal.setAttribute('aria-hidden', 'true');
            if (!document.querySelector('.modal.show')) {
                document.body.classList.remove('modal-open');
            }
            if (this.modalReturnFocus && document.contains(this.modalReturnFocus)) {
                this.modalReturnFocus.focus();
            }
            this.modalReturnFocus = null;
        }
    }

    trapModalFocus(event) {
        const modal = document.querySelector('.modal.show');
        if (!modal) return;

        const focusable = Array.from(modal.querySelectorAll(
            'a[href], button:not(:disabled), input:not(:disabled), ' +
            'select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
        )).filter(element => element.getClientRects().length > 0);

        if (focusable.length === 0) {
            event.preventDefault();
            return;
        }

        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }

    closeTopModal() {
        // Find the currently open modal (with 'show' class)
        const openModal = document.querySelector('.modal.show');
        if (openModal) {
            this.hideModal(openModal.id);
        }
    }

    showLoading() {
        this.pendingRequestCount += 1;
        const overlay = document.getElementById('loading-overlay');
        if (overlay) {
            overlay.classList.remove('hidden');
            overlay.setAttribute('aria-hidden', 'false');
        }
        const main = document.getElementById('main-content');
        if (main) main.setAttribute('aria-busy', 'true');
    }

    hideLoading() {
        this.pendingRequestCount = Math.max(0, this.pendingRequestCount - 1);
        if (this.pendingRequestCount > 0) return;

        const overlay = document.getElementById('loading-overlay');
        if (overlay) {
            overlay.classList.add('hidden');
            overlay.setAttribute('aria-hidden', 'true');
        }
        const main = document.getElementById('main-content');
        if (main) main.setAttribute('aria-busy', 'false');
    }

    setConnectionStatus(connected, message = '') {
        const banner = document.getElementById('connection-banner');
        const messageElement = document.getElementById('connection-message');
        if (!banner) return;

        if (connected) {
            banner.classList.add('hidden');
            return;
        }

        if (messageElement && message) messageElement.textContent = message;
        banner.classList.remove('hidden');
    }

    showToast(type, title, message) {
        const container = document.getElementById('toast-container');
        if (!container) return;

        const icons = {
            success: 'circle-check',
            error: 'circle-x',
            warning: 'triangle-alert',
            info: 'info'
        };
        const safeType = Object.prototype.hasOwnProperty.call(icons, type) ? type : 'info';
        const toastId = `toast-${Date.now()}-${this.toastCounter++}`;

        const toast = document.createElement('div');
        toast.id = toastId;
        toast.className = `toast ${safeType}`;
        toast.setAttribute('role', safeType === 'error' ? 'alert' : 'status');

        const icon = document.createElement('div');
        icon.className = 'toast-icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.innerHTML = this.icon(icons[safeType]);

        const content = document.createElement('div');
        content.className = 'toast-content';

        const titleElement = document.createElement('div');
        titleElement.className = 'toast-title';
        titleElement.textContent = String(title ?? 'Notice');

        const messageElement = document.createElement('div');
        messageElement.className = 'toast-message';
        messageElement.textContent = String(message ?? '');

        const closeButton = document.createElement('button');
        closeButton.className = 'toast-close';
        closeButton.type = 'button';
        closeButton.setAttribute('aria-label', 'Dismiss notification');
        closeButton.innerHTML = this.icon('x');
        closeButton.addEventListener('click', () => this.hideToast(toastId));

        content.append(titleElement, messageElement);
        toast.append(icon, content, closeButton);

        while (container.children.length >= 5) {
            container.firstElementChild.remove();
        }

        container.appendChild(toast);

        // Auto-hide after 5 seconds
        setTimeout(() => this.hideToast(toastId), 5000);
    }

    hideToast(toastId) {
        const toast = document.getElementById(toastId);
        if (toast) {
            toast.style.animation = 'slideOut 0.3s ease-in';
            setTimeout(() => toast.remove(), 300);
        }
    }
}

// Global app instance
let app;

// Initialize the core application even when optional third-party scripts fail.
document.addEventListener('DOMContentLoaded', function() {
    app = new TikTokRecorderApp();
    window.app = app;
});
