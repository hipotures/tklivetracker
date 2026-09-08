/**
 * Analytics page functionality
 */

TikTokRecorderApp.prototype.getAnalyticsToday = function() {
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
};

TikTokRecorderApp.prototype.setupAnalyticsEventListeners = function() {
    const analyticsTabButtons = document.querySelectorAll('.analytics-tab');
    analyticsTabButtons.forEach(tab => {
        tab.addEventListener('click', (event) => {
            this.switchAnalyticsTab(event.currentTarget.dataset.tab);
        });
    });

    const analyticsView = document.getElementById('analytics-view');
    if (analyticsView) {
        analyticsView.addEventListener('change', (event) => {
            this.analyticsView = event.target.value;
            this.analyticsDate = this.getAnalyticsToday();
            this.savePreferences({ analyticsView: this.analyticsView });
            this.loadCurrentAnalyticsTab();
        });
    }

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
        const response = await this.apiRequest(
            `/api/analytics/live-activity?view=${encodeURIComponent(this.analyticsView)}&date=${encodeURIComponent(this.analyticsDate)}`
        );
        if (!response.success) {
            console.error('Analytics API error:', response.error);
            this.showToast('error', 'Error', 'Failed to load analytics data');
            return;
        }

        this.analyticsView = response.view_type;
        this.analyticsDate = response.date;
        this.analyticsNavigation = response.navigation;
        this.updateAnalyticsSummary(response);
        this.updateAnalyticsChart(response);
        this.updateAnalyticsDateDisplay(response);
        this.updateAnalyticsNavigation(response);
    } catch (error) {
        console.error('Analytics load error:', error);
        this.showToast('error', 'Error', 'Failed to load analytics data');
    }
};

TikTokRecorderApp.prototype.formatAnalyticsMetric = function(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '--';
    return Number.isInteger(numeric) ? String(numeric) : numeric.toFixed(1);
};

TikTokRecorderApp.prototype.updateAnalyticsSummary = function(data) {
    const totalSessions = data.totals.total_live_sessions;
    const uniqueUsers = data.totals.total_unique_users;
    const summary = data.summary || {};
    const aggregationLabel = summary.aggregation_label || 'Period';
    const peak = summary.peak;

    document.getElementById('analytics-total-sessions').textContent = totalSessions;
    document.getElementById('analytics-unique-users').textContent = uniqueUsers;
    document.getElementById('analytics-avg-sessions').textContent =
        this.formatAnalyticsMetric(summary.average_per_period);
    document.getElementById('analytics-avg-label').textContent =
        `Avg Starts/${aggregationLabel}`;
    document.getElementById('analytics-peak-label').textContent =
        `Peak ${aggregationLabel}`;

    const peakPeriod = document.getElementById('analytics-peak-period');
    const peakCount = document.getElementById('analytics-peak-count');
    if (peak) {
        peakPeriod.textContent = peak.display_label;
        peakCount.textContent = `${peak.count} ${peak.count === 1 ? 'start' : 'starts'}`;
    } else {
        peakPeriod.textContent = '--';
        peakCount.textContent = 'No live starts';
    }
};

TikTokRecorderApp.prototype.analyticsTooltipTitle = function(context) {
    return context.length ? context[0].label : '';
};

TikTokRecorderApp.prototype.updateAnalyticsChart = function(data) {
    if (!this.ensureChartLibrary()) return;

    const canvas = document.getElementById('analytics-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    if (this.analyticsChart) {
        this.analyticsChart.destroy();
    }

    const labels = data.data.map(item => item.display_label);
    const startsData = data.data.map(item => item.live_count);
    const usersData = data.data.map(item => item.unique_users);

    this.analyticsChart = new Chart(ctx, {
        data: {
            labels,
            datasets: [
                {
                    type: 'bar',
                    label: 'Live Starts',
                    data: startsData,
                    borderColor: '#3b82f6',
                    backgroundColor: 'rgba(59, 130, 246, 0.28)',
                    borderWidth: 1,
                    borderRadius: 3,
                    maxBarThickness: 44
                },
                {
                    type: 'line',
                    label: 'Distinct Users',
                    data: usersData,
                    borderColor: '#10b981',
                    backgroundColor: '#10b981',
                    borderWidth: 2,
                    fill: false,
                    tension: 0.15,
                    pointRadius: 2,
                    pointHoverRadius: 4,
                    spanGaps: false
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
                        color: '#748196',
                        precision: 0
                    }
                },
                x: {
                    grid: {
                        display: false
                    },
                    ticks: {
                        color: '#748196',
                        maxRotation: 0,
                        autoSkip: true,
                        maxTicksLimit: 16
                    }
                }
            },
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    backgroundColor: '#111b2b',
                    titleColor: '#ffffff',
                    bodyColor: '#ffffff',
                    cornerRadius: 6,
                    displayColors: true,
                    callbacks: {
                        title: (context) => this.analyticsTooltipTitle(context),
                        label: function(context) {
                            if (context.raw === null) return `${context.dataset.label}: not observed yet`;
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
    if (dateDisplay && data.period) {
        dateDisplay.textContent = data.period.range_label;
    }
};

TikTokRecorderApp.prototype.updateAnalyticsNavigation = function(data) {
    const prevBtn = document.getElementById('analytics-prev');
    const nextBtn = document.getElementById('analytics-next');
    this.analyticsNavigation = data.navigation || null;

    if (prevBtn) {
        prevBtn.disabled = !data.navigation?.has_prev;
        prevBtn.classList.toggle('disabled', prevBtn.disabled);
    }
    if (nextBtn) {
        nextBtn.disabled = !data.navigation?.has_next;
        nextBtn.classList.toggle('disabled', nextBtn.disabled);
    }
};

TikTokRecorderApp.prototype.navigateAnalyticsPrev = function() {
    const target = this.analyticsNavigation?.prev;
    if (!target || !this.analyticsNavigation?.has_prev) return;
    this.analyticsDate = target;
    this.loadCurrentAnalyticsTab();
};

TikTokRecorderApp.prototype.navigateAnalyticsNext = function() {
    const target = this.analyticsNavigation?.next;
    if (!target || !this.analyticsNavigation?.has_next) return;
    this.analyticsDate = target;
    this.loadCurrentAnalyticsTab();
};

TikTokRecorderApp.prototype.switchAnalyticsTab = function(tabName) {
    if (!['live-sessions', 'new-users'].includes(tabName)) {
        tabName = 'live-sessions';
    }

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

    document.querySelectorAll('.analytics-tab-content').forEach(content => {
        content.classList.remove('active');
        content.hidden = true;
    });
    const activeContent = document.getElementById(`${tabName}-tab`);
    if (activeContent) {
        activeContent.classList.add('active');
        activeContent.hidden = false;
    }

    this.currentAnalyticsTab = tabName;
    this.savePreferences({ currentAnalyticsTab: tabName });
    this.loadCurrentAnalyticsTab();
};

TikTokRecorderApp.prototype.loadCurrentAnalyticsTab = function() {
    const currentTab = this.currentAnalyticsTab || 'live-sessions';
    if (currentTab === 'new-users') {
        this.loadNewUsersAnalytics();
    } else {
        this.loadAnalytics();
    }
};

TikTokRecorderApp.prototype.loadNewUsersAnalytics = async function() {
    try {
        const response = await this.apiRequest(
            `/api/analytics/new-users?view=${encodeURIComponent(this.analyticsView)}&date=${encodeURIComponent(this.analyticsDate)}`
        );
        if (!response.success) {
            console.error('New users analytics API error:', response.error);
            this.showToast('error', 'Error', 'Failed to load tracked-user analytics data');
            return;
        }

        this.analyticsView = response.view_type;
        this.analyticsDate = response.date;
        this.analyticsNavigation = response.navigation;
        this.updateNewUsersChart(response);
        this.updateAnalyticsDateDisplay(response);
        this.updateAnalyticsNavigation(response);
    } catch (error) {
        console.error('New users analytics load error:', error);
        this.showToast('error', 'Error', 'Failed to load tracked-user analytics data');
    }
};

TikTokRecorderApp.prototype.updateNewUsersChart = function(data) {
    if (!this.ensureChartLibrary()) return;

    const canvas = document.getElementById('new-users-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    if (this.newUsersChart) {
        this.newUsersChart.destroy();
    }

    const labels = data.data.map(item => item.display_label);
    const newUsersData = data.data.map(item => item.new_users_count);

    this.newUsersChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [
                {
                    label: 'Tracked Users Added',
                    data: newUsersData,
                    borderColor: '#4f9cf9',
                    backgroundColor: 'rgba(79, 156, 249, 0.28)',
                    borderWidth: 1,
                    borderRadius: 3,
                    maxBarThickness: 44
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
                        color: '#748196',
                        precision: 0
                    }
                },
                x: {
                    grid: {
                        display: false
                    },
                    ticks: {
                        color: '#748196',
                        maxRotation: 0,
                        autoSkip: true,
                        maxTicksLimit: 16
                    }
                }
            },
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    backgroundColor: '#111b2b',
                    titleColor: '#ffffff',
                    bodyColor: '#ffffff',
                    cornerRadius: 6,
                    displayColors: true,
                    callbacks: {
                        title: (context) => this.analyticsTooltipTitle(context),
                        label: function(context) {
                            if (context.raw === null) return 'Tracked Users Added: not observed yet';
                            return `Tracked Users Added: ${context.raw}`;
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
