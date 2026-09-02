/**
 * Analytics page functionality
 */

// Analytics methods for TikTokRecorderApp
TikTokRecorderApp.prototype.setupAnalyticsEventListeners = function() {
    // Analytics tabs
    const analyticsTabButtons = document.querySelectorAll('.analytics-tab');
    analyticsTabButtons.forEach(tab => {
        tab.addEventListener('click', (e) => {
            const tabName = e.currentTarget.dataset.tab;
            this.switchAnalyticsTab(tabName);
        });
    });

    // Analytics view change
    const analyticsView = document.getElementById('analytics-view');
    if (analyticsView) {
        analyticsView.addEventListener('change', (e) => {
            this.analyticsView = e.target.value;
            this.savePreferences({ analyticsView: e.target.value });
            // Reset to current date when changing view
            this.analyticsDate = new Date().toISOString().split('T')[0];
            this.loadCurrentAnalyticsTab();
        });
    }

    // Navigation buttons
    const analyticsPrev = document.getElementById('analytics-prev');
    const analyticsNext = document.getElementById('analytics-next');
    const analyticsRefresh = document.getElementById('analytics-refresh');

    if (analyticsPrev) {
        analyticsPrev.addEventListener('click', () => this.navigateAnalyticsPrev());
    }

    if (analyticsNext) {
        analyticsNext.addEventListener('click', () => this.navigateAnalyticsNext());
    }

    if (analyticsRefresh) {
        analyticsRefresh.addEventListener('click', () => this.loadCurrentAnalyticsTab());
    }

    const reloadChartLibrary = document.getElementById('reload-chart-library');
    if (reloadChartLibrary) {
        reloadChartLibrary.addEventListener('click', () => window.location.reload());
    }
};

TikTokRecorderApp.prototype.loadAnalytics = async function() {
    try {
        const response = await this.apiRequest(`/api/analytics/live-activity?view=${this.analyticsView}&date=${this.analyticsDate}`);
        if (response.success) {
            this.updateAnalyticsSummary(response);
            this.updateAnalyticsChart(response);
            this.updateAnalyticsDateDisplay(response);
            this.updateAnalyticsNavigation(response);
        } else {
            console.error('Analytics API error:', response.error);
            this.showToast('error', 'Error', 'Failed to load analytics data');
        }
    } catch (error) {
        console.error('Analytics load error:', error);
        this.showToast('error', 'Error', 'Failed to load analytics data');
    }
};

TikTokRecorderApp.prototype.updateAnalyticsSummary = function(data) {
    const totalSessions = data.totals.total_live_sessions;
    const uniqueUsers = data.totals.total_unique_users;
    const avgSessions = totalSessions > 0 && data.data.length > 0 ?
        Math.round(totalSessions / data.data.length) : 0;

    // Find peak period
    let peakPeriod = '--';
    let maxSessions = 0;
    data.data.forEach(item => {
        if (item.live_count > maxSessions) {
            maxSessions = item.live_count;
            peakPeriod = item.display_label;
        }
    });

    document.getElementById('analytics-total-sessions').textContent = totalSessions;
    document.getElementById('analytics-unique-users').textContent = uniqueUsers;
    document.getElementById('analytics-avg-sessions').textContent = avgSessions;
    document.getElementById('analytics-peak-period').textContent = peakPeriod;
};

TikTokRecorderApp.prototype.updateAnalyticsChart = function(data) {
    if (!this.ensureChartLibrary()) return;

    const canvas = document.getElementById('analytics-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    // Destroy existing chart
    if (this.analyticsChart) {
        this.analyticsChart.destroy();
    }

    const labels = data.data.map(item => item.display_label);
    const sessionsData = data.data.map(item => item.live_count);
    const usersData = data.data.map(item => item.unique_users);

    this.analyticsChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Live Sessions',
                    data: sessionsData,
                    borderColor: '#3b82f6',
                    backgroundColor: 'rgba(59, 130, 246, 0.1)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4
                },
                {
                    label: 'Unique Users',
                    data: usersData,
                    borderColor: '#10b981',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index'
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: {
                        color: 'rgba(148, 163, 184, 0.12)'
                    },
                    ticks: {
                        color: '#748196'
                    }
                },
                x: {
                    grid: {
                        color: 'rgba(148, 163, 184, 0.08)'
                    },
                    ticks: {
                        color: '#748196'
                    }
                }
            },
            plugins: {
                legend: {
                    display: false // We have custom legend
                },
                tooltip: {
                    backgroundColor: '#111b2b',
                    titleColor: '#ffffff',
                    bodyColor: '#ffffff',
                    cornerRadius: 6,
                    displayColors: true,
                    callbacks: {
                        title: function(context) {
                            return `Period: ${context[0].label}`;
                        },
                        label: function(context) {
                            return `${context.dataset.label}: ${context.raw}`;
                        }
                    }
                }
            }
        }
    });
};

TikTokRecorderApp.prototype.updateAnalyticsDateDisplay = function(data) {
    const dateDisplay = document.getElementById('analytics-date-display');
    let displayText = '';

    switch (this.analyticsView) {
        case 'hourly':
            displayText = new Date(this.analyticsDate).toLocaleDateString('en-US', {
                weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
            });
            break;
        case 'daily':
            // Show 30-day period ending on selected date
            const endDate = new Date(this.analyticsDate);
            const startDate = new Date(endDate);
            startDate.setDate(endDate.getDate() - 29);
            displayText = `${startDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} - ${endDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}`;
            break;
        case 'weekly':
            // Show quarter ending in selected month
            const selectedDate = new Date(this.analyticsDate);
            const endMonth = selectedDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
            const startMonth = new Date(selectedDate);
            startMonth.setMonth(selectedDate.getMonth() - 2);
            const startMonthName = startMonth.toLocaleDateString('en-US', { month: 'long' });
            displayText = `${startMonthName} - ${endMonth}`;
            break;
        case 'last24h':
            displayText = 'Last 24 Hours';
            break;
        case 'last7d':
            displayText = 'Last 7 Days';
            break;
        case 'last30d':
            displayText = 'Last 30 Days';
            break;
        case 'last365d':
            displayText = 'Last 1 Year';
            break;
    }

    if (dateDisplay) {
        dateDisplay.textContent = displayText;
    }
};

TikTokRecorderApp.prototype.updateAnalyticsNavigation = function(data) {
    const prevBtn = document.getElementById('analytics-prev');
    const nextBtn = document.getElementById('analytics-next');

    if (prevBtn && nextBtn) {
        // Enable/disable buttons based on data availability
        if (data.navigation.has_prev) {
            prevBtn.disabled = false;
            prevBtn.classList.remove('disabled');
        } else {
            prevBtn.disabled = true;
            prevBtn.classList.add('disabled');
        }

        if (data.navigation.has_next) {
            nextBtn.disabled = false;
            nextBtn.classList.remove('disabled');
        } else {
            nextBtn.disabled = true;
            nextBtn.classList.add('disabled');
        }
    }
};

TikTokRecorderApp.prototype.navigateAnalyticsPrev = function() {
    const currentDate = new Date(this.analyticsDate);

    switch (this.analyticsView) {
        case 'hourly':
            currentDate.setDate(currentDate.getDate() - 1);
            break;
        case 'daily':
            currentDate.setDate(currentDate.getDate() - 30);
            break;
        case 'weekly':
            currentDate.setDate(1);
            currentDate.setMonth(currentDate.getMonth() - 3);
            break;
        case 'last24h':
        case 'last7d':
        case 'last30d':
        case 'last365d':
            // Navigation disabled for "last" views
            return;
    }

    this.analyticsDate = currentDate.toISOString().split('T')[0];
    this.loadCurrentAnalyticsTab();
};

// Tab Management
TikTokRecorderApp.prototype.switchAnalyticsTab = function(tabName) {
    if (!['live-sessions', 'new-users'].includes(tabName)) {
        tabName = 'live-sessions';
    }

    // Update active tab button
    document.querySelectorAll('.analytics-tab').forEach(tab => {
        tab.classList.remove('active');
        tab.setAttribute('aria-selected', 'false');
    });
    const activeTab = Array.from(document.querySelectorAll('.analytics-tab')).find(
        tab => tab.dataset.tab === tabName
    );
    if (activeTab) {
        activeTab.classList.add('active');
        activeTab.setAttribute('aria-selected', 'true');
    }

    // Update active tab content
    document.querySelectorAll('.analytics-tab-content').forEach(content => {
        content.classList.remove('active');
        content.hidden = true;
    });
    const activeContent = document.getElementById(`${tabName}-tab`);
    if (activeContent) {
        activeContent.classList.add('active');
        activeContent.hidden = false;
    }

    // Save current tab preference
    this.currentAnalyticsTab = tabName;
    this.savePreferences({ currentAnalyticsTab: tabName });

    // Load data for the selected tab
    this.loadCurrentAnalyticsTab();
};

TikTokRecorderApp.prototype.loadCurrentAnalyticsTab = function() {
    const currentTab = this.currentAnalyticsTab || 'live-sessions';

    if (currentTab === 'live-sessions') {
        this.loadAnalytics();
    } else if (currentTab === 'new-users') {
        this.loadNewUsersAnalytics();
    }
};

// New Users Analytics
TikTokRecorderApp.prototype.loadNewUsersAnalytics = async function() {
    try {
        const response = await this.apiRequest(`/api/analytics/new-users?view=${this.analyticsView}&date=${this.analyticsDate}`);
        if (response.success) {
            this.updateNewUsersChart(response);
            this.updateAnalyticsDateDisplay(response);
            this.updateAnalyticsNavigation(response);
        } else {
            console.error('New Users Analytics API error:', response.error);
            this.showToast('error', 'Error', 'Failed to load new users analytics data');
        }
    } catch (error) {
        console.error('New Users Analytics load error:', error);
        this.showToast('error', 'Error', 'Failed to load new users analytics data');
    }
};

TikTokRecorderApp.prototype.updateNewUsersChart = function(data) {
    if (!this.ensureChartLibrary()) return;

    const canvas = document.getElementById('new-users-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    // Destroy existing chart
    if (this.newUsersChart) {
        this.newUsersChart.destroy();
    }

    const labels = data.data.map(item => item.display_label);
    const newUsersData = data.data.map(item => item.new_users_count);

    this.newUsersChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'New Users',
                    data: newUsersData,
                    borderColor: '#4f9cf9',
                    backgroundColor: 'rgba(79, 156, 249, 0.1)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index'
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: {
                        color: 'rgba(148, 163, 184, 0.12)'
                    },
                    ticks: {
                        color: '#748196'
                    }
                },
                x: {
                    grid: {
                        color: 'rgba(148, 163, 184, 0.08)'
                    },
                    ticks: {
                        color: '#748196'
                    }
                }
            },
            plugins: {
                legend: {
                    display: false // We have custom legend
                },
                tooltip: {
                    backgroundColor: '#111b2b',
                    titleColor: '#ffffff',
                    bodyColor: '#ffffff',
                    cornerRadius: 6,
                    displayColors: true,
                    callbacks: {
                        title: function(context) {
                            return `Period: ${context[0].label}`;
                        },
                        label: function(context) {
                            return `${context.dataset.label}: ${context.raw}`;
                        }
                    }
                }
            }
        }
    });
};

TikTokRecorderApp.prototype.ensureChartLibrary = function() {
    const error = document.getElementById('chart-library-error');
    const available = typeof Chart !== 'undefined';

    if (error) error.classList.toggle('hidden', available);
    return available;
};

TikTokRecorderApp.prototype.navigateAnalyticsNext = function() {
    const currentDate = new Date(this.analyticsDate);

    switch (this.analyticsView) {
        case 'hourly':
            currentDate.setDate(currentDate.getDate() + 1);
            break;
        case 'daily':
            currentDate.setDate(currentDate.getDate() + 30);
            break;
        case 'weekly':
            currentDate.setDate(1);
            currentDate.setMonth(currentDate.getMonth() + 3);
            break;
        case 'last24h':
        case 'last7d':
        case 'last30d':
        case 'last365d':
            // Navigation disabled for "last" views
            return;
    }

    this.analyticsDate = currentDate.toISOString().split('T')[0];
    this.loadCurrentAnalyticsTab();
};
