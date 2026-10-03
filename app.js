// Financial Tracker Frontend Logic
document.addEventListener('alpine:init', () => {
    Alpine.data('app', () => ({
        // State
        activeTab: 'dashboard', // dashboard, transactions, analytics, goals, settings
        auth: {
            hasPin: false,
            isAuthenticated: false,
            pinInput: '',
            newPinInput: '',
            error: ''
        },
        token: localStorage.getItem('fin_token') || '',
        baseCurrency: localStorage.getItem('fin_base_curr') || 'USD', // USD, TRY, KZT
        
        // Data from backend
        rates: { USD: 1.0, TRY: 34.5, KZT: 485.0, last_updated: 0, source: 'fallback' },
        customRates: { enabled: false, TRY: '', KZT: '' },
        dashboard: {
            net_worth: { USD: 0, TRY: 0, KZT: 0 },
            month_stats: {
                income: { USD: 0, TRY: 0, KZT: 0 },
                expense: { USD: 0, TRY: 0, KZT: 0 },
                investment: { USD: 0, TRY: 0, KZT: 0 }
            },
            accounts: [],
            top_categories: [],
            recent_transactions: [],
            currency_allocation: { USD: 0, TRY: 0, KZT: 0 },
            budget: null
        },
        accounts: [],
        categories: [],
        transactions: [],
        goals: [],
        analytics: {
            monthly_history: {},
            categories_breakdown: []
        },

        // Personal Monthly Budget (50% Card / 50% Cash)
        budget: {
            amount: 1000,
            currency: 'USD',
            card_percent: 50,
            cash_percent: 50,
            card_budget: 500,
            cash_budget: 500,
            card_spent: 0,
            cash_spent: 0,
            card_remaining: 500,
            cash_remaining: 500,
            total_spent: 0,
            total_remaining: 1000,
            card_progress: 0,
            cash_progress: 0,
            equivalents: {}
        },
        budgetForm: {
            amount: 1000,
            currency: 'USD',
            card_percent: 50,
            cash_percent: 50
        },

        // Filters for Transactions tab
        txFilter: {
            type: '',
            month: '',
            currency: '',
            search: ''
        },

        // Modals state
        modalOpen: false,
        modalType: '', // 'transaction', 'account', 'goal', 'contribute', 'custom_rates', 'budget'
        
        // Form states
        txForm: {
            type: 'expense', // expense, income, investment, transfer
            amount: '',
            currency: 'USD',
            account_id: '',
            to_account_id: '',
            category_id: '',
            date: new Date().toISOString().split('T')[0],
            time: new Date().toTimeString().slice(0, 5),
            note: '',
            goal_id: ''
        },

        accountForm: {
            name: '',
            account_type: 'card', // 'card', 'cash', 'savings'
            currency: 'USD',
            balance: 0,
            icon: 'credit-card',
            color: '#0ea5e9'
        },

        goalForm: {
            title: '',
            target_amount: '',
            currency: 'USD',
            current_amount: 0,
            deadline: '',
            icon: 'target',
            color: '#8b5cf6',
            note: ''
        },

        contributeForm: {
            goal_id: null,
            goal_title: '',
            amount: '',
            account_id: ''
        },

        // Currency converter widget
        converter: {
            amount: 100,
            currency: 'USD'
        },

        // Charts references
        chartMonthly: null,
        chartCategory: null,
        chartAllocation: null,
        loading: false,

        // Initialize App
        async init() {
            if (this.token) {
                await this.checkAuth();
            } else {
                await this.checkAuthStatus();
            }

            if (this.auth.isAuthenticated) {
                await this.loadAllData();
            }

            // Register Service Worker if supported
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.register('/static/sw.js').catch(err => console.log('SW failed:', err));
            }
        },

        // Helper formatting functions
        formatMoney(amount, currency = 'USD') {
            const num = parseFloat(amount || 0);
            if (currency === 'USD') {
                return '$' + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            } else if (currency === 'TRY') {
                return num.toLocaleString('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₺';
            } else if (currency === 'KZT') {
                return num.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + ' ₸';
            }
            return num.toLocaleString() + ' ' + currency;
        },

        getCurrencySymbol(curr) {
            switch(curr) {
                case 'USD': return '$';
                case 'TRY': return '₺';
                case 'KZT': return '₸';
                default: return curr;
            }
        },

        setBaseCurrency(curr) {
            this.baseCurrency = curr;
            localStorage.setItem('fin_base_curr', curr);
            this.$nextTick(() => {
                this.renderCharts();
            });
        },

        // Auth
        async checkAuthStatus() {
            try {
                const res = await fetch('/api/auth/status', {
                    headers: this.token ? { 'Authorization': `Bearer ${this.token}` } : {}
                });
                const data = await res.json();
                this.auth.hasPin = data.has_pin;
                this.auth.isAuthenticated = data.authenticated;
            } catch (e) {
                console.error('Auth status error:', e);
            }
        },

        async checkAuth() {
            try {
                const res = await fetch('/api/auth/status', {
                    headers: { 'Authorization': `Bearer ${this.token}` }
                });
                const data = await res.json();
                this.auth.hasPin = data.has_pin;
                this.auth.isAuthenticated = data.authenticated;
                if (!data.authenticated) {
                    this.token = '';
                    localStorage.removeItem('fin_token');
                }
            } catch (e) {
                this.auth.isAuthenticated = false;
            }
        },

        async submitPin() {
            this.auth.error = '';
            try {
                const res = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pin: this.auth.pinInput })
                });
                if (res.ok) {
                    const data = await res.json();
                    this.token = data.token;
                    localStorage.setItem('fin_token', data.token);
                    this.auth.isAuthenticated = true;
                    this.auth.pinInput = '';
                    await this.loadAllData();
                } else {
                    const err = await res.json();
                    this.auth.error = err.detail || 'Неверный PIN-код';
                }
            } catch (e) {
                this.auth.error = 'Ошибка подключения к серверу';
            }
        },

        async setupPin() {
            this.auth.error = '';
            if (this.auth.newPinInput.length < 4) {
                this.auth.error = 'PIN должен состоять минимум из 4 символов';
                return;
            }
            try {
                const res = await fetch('/api/auth/setup', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pin: this.auth.newPinInput })
                });
                if (res.ok) {
                    const data = await res.json();
                    this.token = data.token;
                    localStorage.setItem('fin_token', data.token);
                    this.auth.hasPin = true;
                    this.auth.isAuthenticated = true;
                    this.auth.newPinInput = '';
                    await this.loadAllData();
                } else {
                    const err = await res.json();
                    this.auth.error = err.detail || 'Ошибка создания PIN';
                }
            } catch (e) {
                this.auth.error = 'Ошибка запроса';
            }
        },

        async removePin() {
            if (!confirm('Вы уверены, что хотите отключить защиту по PIN-коду?')) return;
            await this.apiCall('/api/auth/remove-pin', 'POST');
            this.auth.hasPin = false;
            this.token = '';
            localStorage.removeItem('fin_token');
            alert('PIN-код успешно отключен');
        },

        lockApp() {
            this.token = '';
            localStorage.removeItem('fin_token');
            this.auth.isAuthenticated = false;
            this.auth.pinInput = '';
        },

        // API Caller Helper
        async apiCall(url, method = 'GET', body = null) {
            const headers = {
                'Content-Type': 'application/json'
            };
            if (this.token) {
                headers['Authorization'] = `Bearer ${this.token}`;
            }
            const opts = { method, headers };
            if (body) {
                opts.body = JSON.stringify(body);
            }
            const res = await fetch(url, opts);
            if (res.status === 401) {
                this.auth.isAuthenticated = false;
                throw new Error('Unauthorized');
            }
            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: 'Request failed' }));
                throw new Error(err.detail || 'Error');
            }
            return await res.json();
        },

        // Load Data
        async loadAllData() {
            this.loading = true;
            try {
                await Promise.all([
                    this.loadDashboard(),
                    this.loadAccounts(),
                    this.loadCategories(),
                    this.loadTransactions(),
                    this.loadGoals(),
                    this.loadRates(),
                    this.loadBudget()
                ]);
                this.$nextTick(() => {
                    if (window.lucide) window.lucide.createIcons();
                });
            } catch (e) {
                console.error('Error loading data:', e);
            } finally {
                this.loading = false;
            }
        },

        async loadRates() {
            try {
                const data = await this.apiCall('/api/rates');
                this.rates = data.rates;
                if (data.custom) {
                    this.customRates = data.custom;
                }
            } catch (e) {
                console.error(e);
            }
        },

        async loadDashboard() {
            try {
                this.dashboard = await this.apiCall('/api/dashboard');
                if (this.dashboard.rates) {
                    this.rates = this.dashboard.rates;
                }
                if (this.dashboard.budget) {
                    this.budget = this.dashboard.budget;
                }
            } catch (e) {
                console.error(e);
            }
        },

        async loadBudget() {
            try {
                this.budget = await this.apiCall('/api/budget');
            } catch (e) {
                console.error('Error loading budget:', e);
            }
        },

        openBudgetModal() {
            this.budgetForm = {
                amount: this.budget.amount || 1000,
                currency: this.budget.currency || 'USD',
                card_percent: this.budget.card_percent || 50,
                cash_percent: this.budget.cash_percent || 50
            };
            this.modalType = 'budget';
            this.modalOpen = true;
        },

        async saveBudget() {
            if (!this.budgetForm.amount || parseFloat(this.budgetForm.amount) <= 0) {
                alert('Укажите корректную сумму лимита');
                return;
            }
            try {
                this.budget = await this.apiCall('/api/budget', 'POST', {
                    amount: parseFloat(this.budgetForm.amount),
                    currency: this.budgetForm.currency,
                    card_percent: parseFloat(this.budgetForm.card_percent || 50),
                    cash_percent: 100 - parseFloat(this.budgetForm.card_percent || 50)
                });
                this.closeModal();
                await this.loadDashboard();
            } catch (e) {
                alert('Ошибка сохранения лимита: ' + e.message);
            }
        },

        async loadAccounts() {
            try {
                this.accounts = await this.apiCall('/api/accounts');
                if (this.accounts.length > 0 && !this.txForm.account_id) {
                    this.txForm.account_id = this.accounts[0].id;
                }
            } catch (e) {
                console.error(e);
            }
        },

        async loadCategories() {
            try {
                this.categories = await this.apiCall('/api/categories');
                const defaultCat = this.categories.find(c => c.type === this.txForm.type);
                if (defaultCat) this.txForm.category_id = defaultCat.id;
            } catch (e) {
                console.error(e);
            }
        },

        async loadTransactions() {
            try {
                let url = '/api/transactions?';
                if (this.txFilter.type) url += `type=${this.txFilter.type}&`;
                if (this.txFilter.month) url += `month=${this.txFilter.month}&`;
                if (this.txFilter.currency) url += `currency=${this.txFilter.currency}&`;
                if (this.txFilter.search) url += `search=${encodeURIComponent(this.txFilter.search)}&`;
                this.transactions = await this.apiCall(url);
            } catch (e) {
                console.error(e);
            }
        },

        async loadGoals() {
            try {
                this.goals = await this.apiCall('/api/goals');
            } catch (e) {
                console.error(e);
            }
        },

        async loadAnalytics() {
            try {
                this.analytics = await this.apiCall('/api/analytics?months=6');
                this.$nextTick(() => {
                    this.renderCharts();
                });
            } catch (e) {
                console.error(e);
            }
        },

        // Navigation
        setTab(tab) {
            this.activeTab = tab;
            if (tab === 'analytics') {
                this.loadAnalytics();
            } else if (tab === 'transactions') {
                this.loadTransactions();
            } else if (tab === 'dashboard') {
                this.loadDashboard();
            } else if (tab === 'goals') {
                this.loadGoals();
            }
            this.$nextTick(() => {
                if (window.lucide) window.lucide.createIcons();
            });
        },

        // Modal Openers
        openAddTxModal(type = 'expense') {
            this.txForm.type = type;
            this.txForm.amount = '';
            this.txForm.note = '';
            this.txForm.date = new Date().toISOString().split('T')[0];
            this.txForm.time = new Date().toTimeString().slice(0, 5);
            this.txForm.goal_id = '';
            
            // Set currency from matching account
            if (this.accounts.length > 0) {
                if (!this.txForm.account_id) this.txForm.account_id = this.accounts[0].id;
                const acc = this.accounts.find(a => a.id == this.txForm.account_id);
                if (acc) this.txForm.currency = acc.currency;
            }

            // Set default category for this type
            const matched = this.categories.filter(c => c.type === type);
            if (matched.length > 0) {
                this.txForm.category_id = matched[0].id;
            }

            this.modalType = 'transaction';
            this.modalOpen = true;
            this.$nextTick(() => {
                if (window.lucide) window.lucide.createIcons();
            });
        },

        openAddAccountModal() {
            this.accountForm = {
                name: '',
                account_type: 'card',
                currency: 'USD',
                balance: 0,
                icon: 'credit-card',
                color: '#0ea5e9'
            };
            this.modalType = 'account';
            this.modalOpen = true;
        },

        openAddGoalModal() {
            this.goalForm = {
                title: '',
                target_amount: '',
                currency: 'USD',
                current_amount: 0,
                deadline: '',
                icon: 'target',
                color: '#8b5cf6',
                note: ''
            };
            this.modalType = 'goal';
            this.modalOpen = true;
        },

        openContributeModal(goal) {
            this.contributeForm = {
                goal_id: goal.id,
                goal_title: goal.title,
                amount: '',
                account_id: this.accounts.length > 0 ? this.accounts[0].id : ''
            };
            this.modalType = 'contribute';
            this.modalOpen = true;
        },

        closeModal() {
            this.modalOpen = false;
        },

        // Form Submit Actions
        async submitTransaction() {
            if (!this.txForm.amount || parseFloat(this.txForm.amount) <= 0) {
                alert('Пожалуйста, укажите корректную сумму');
                return;
            }

            const payload = {
                type: this.txForm.type,
                amount: parseFloat(this.txForm.amount),
                currency: this.txForm.currency,
                account_id: this.txForm.account_id ? parseInt(this.txForm.account_id) : null,
                to_account_id: this.txForm.type === 'transfer' && this.txForm.to_account_id ? parseInt(this.txForm.to_account_id) : null,
                category_id: this.txForm.type !== 'transfer' && this.txForm.category_id ? parseInt(this.txForm.category_id) : null,
                date: this.txForm.date,
                time: this.txForm.time,
                note: this.txForm.note,
                goal_id: this.txForm.goal_id ? parseInt(this.txForm.goal_id) : null
            };

            try {
                await this.apiCall('/api/transactions', 'POST', payload);
                this.closeModal();
                await this.loadAllData();
            } catch (e) {
                alert('Ошибка сохранения операции: ' + e.message);
            }
        },

        async deleteTx(id) {
            if (!confirm('Удалить эту операцию? Баланс счета будет скорректирован обратно.')) return;
            try {
                await this.apiCall(`/api/transactions/${id}`, 'DELETE');
                await this.loadAllData();
            } catch (e) {
                alert('Ошибка при удалении: ' + e.message);
            }
        },

        async submitAccount() {
            if (!this.accountForm.name) {
                alert('Введите название счета');
                return;
            }
            try {
                await this.apiCall('/api/accounts', 'POST', {
                    ...this.accountForm,
                    balance: parseFloat(this.accountForm.balance || 0)
                });
                this.closeModal();
                await this.loadAllData();
            } catch (e) {
                alert('Ошибка создания счета: ' + e.message);
            }
        },

        async deleteAccount(id) {
            if (!confirm('Вы уверены, что хотите удалить этот счет?')) return;
            try {
                await this.apiCall(`/api/accounts/${id}`, 'DELETE');
                await this.loadAllData();
            } catch (e) {
                alert('Ошибка: ' + e.message);
            }
        },

        async submitGoal() {
            if (!this.goalForm.title || !this.goalForm.target_amount) {
                alert('Укажите название и целевую сумму');
                return;
            }
            try {
                await this.apiCall('/api/goals', 'POST', {
                    ...this.goalForm,
                    target_amount: parseFloat(this.goalForm.target_amount),
                    current_amount: parseFloat(this.goalForm.current_amount || 0)
                });
                this.closeModal();
                await this.loadGoals();
            } catch (e) {
                alert('Ошибка: ' + e.message);
            }
        },

        async submitContribute() {
            if (!this.contributeForm.amount || parseFloat(this.contributeForm.amount) <= 0) {
                alert('Укажите сумму пополнения');
                return;
            }
            try {
                await this.apiCall(`/api/goals/${this.contributeForm.goal_id}/contribute`, 'POST', {
                    amount: parseFloat(this.contributeForm.amount),
                    account_id: this.contributeForm.account_id ? parseInt(this.contributeForm.account_id) : null
                });
                this.closeModal();
                await this.loadAllData();
            } catch (e) {
                alert('Ошибка: ' + e.message);
            }
        },

        async deleteGoal(id) {
            if (!confirm('Удалить эту цель?')) return;
            try {
                await this.apiCall(`/api/goals/${id}`, 'DELETE');
                await this.loadGoals();
            } catch (e) {
                alert('Ошибка: ' + e.message);
            }
        },

        // Custom Rates Setting
        async saveCustomRates() {
            try {
                const payload = {
                    TRY: this.customRates.TRY ? parseFloat(this.customRates.TRY) : null,
                    KZT: this.customRates.KZT ? parseFloat(this.customRates.KZT) : null,
                    enabled: this.customRates.enabled
                };
                await this.apiCall('/api/rates/custom', 'POST', payload);
                alert('Курсы валют сохранены!');
                await this.loadAllData();
            } catch (e) {
                alert('Ошибка: ' + e.message);
            }
        },

        // Quick conversion live calculator helper
        getConvertedValues(amount, fromCurr) {
            const amt = parseFloat(amount || 0);
            const rateUSD = 1.0;
            const rateTRY = parseFloat(this.rates.TRY || 34.5);
            const rateKZT = parseFloat(this.rates.KZT || 485.0);

            let inUSD = amt;
            if (fromCurr === 'TRY') inUSD = amt / rateTRY;
            else if (fromCurr === 'KZT') inUSD = amt / rateKZT;

            return {
                USD: round(inUSD, 2),
                TRY: round(inUSD * rateTRY, 2),
                KZT: round(inUSD * rateKZT, 2)
            };
        },

        // Backup Export / Import
        async exportBackup() {
            try {
                const data = await this.apiCall('/api/backup');
                const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `finances_backup_${new Date().toISOString().slice(0, 10)}.json`;
                a.click();
                URL.revokeObjectURL(url);
            } catch (e) {
                alert('Ошибка экспорта: ' + e.message);
            }
        },

        async importBackup(e) {
            const file = e.target.files[0];
            if (!file) return;
            if (!confirm('Импорт заменит текущие данные. Продолжить?')) return;

            const reader = new FileReader();
            reader.onload = async (evt) => {
                try {
                    const json = JSON.parse(evt.target.result);
                    await this.apiCall('/api/restore', 'POST', json);
                    alert('Данные успешно импортированы!');
                    await this.loadAllData();
                } catch (err) {
                    alert('Ошибка импорта: ' + err.message);
                }
            };
            reader.readAsText(file);
        },

        // Render Analytics Charts with Chart.js
        renderCharts() {
            if (!window.Chart) return;
            const textColor = '#94a3b8';
            const gridColor = '#1e293b';

            // 1. Monthly History Bar Chart
            const ctxMonthly = document.getElementById('chartMonthly');
            if (ctxMonthly && this.analytics.monthly_history) {
                if (this.chartMonthly) this.chartMonthly.destroy();

                const labels = Object.keys(this.analytics.monthly_history);
                const currKey = this.baseCurrency.toLowerCase(); // 'usd', 'try', 'kzt'
                
                const incomeData = labels.map(m => this.analytics.monthly_history[m][`income_${currKey}`] || 0);
                const expenseData = labels.map(m => this.analytics.monthly_history[m][`expense_${currKey}`] || 0);
                const investData = labels.map(m => this.analytics.monthly_history[m][`investment_${currKey}`] || 0);

                this.chartMonthly = new Chart(ctxMonthly, {
                    type: 'bar',
                    data: {
                        labels: labels.map(l => {
                            const [y, m] = l.split('-');
                            const months = ['Янв','Фев','Мар','Апр','Май','Июн','Июл','Авг','Сен','Окт','Ноя','Дек'];
                            return months[parseInt(m)-1] + ' ' + y.slice(2);
                        }),
                        datasets: [
                            {
                                label: 'Доходы',
                                data: incomeData,
                                backgroundColor: '#10b981',
                                borderRadius: 6
                            },
                            {
                                label: 'Расходы',
                                data: expenseData,
                                backgroundColor: '#f43f5e',
                                borderRadius: 6
                            },
                            {
                                label: 'Вложения / Накопления',
                                data: investData,
                                backgroundColor: '#8b5cf6',
                                borderRadius: 6
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                labels: { color: textColor, font: { family: 'Inter', size: 12 } }
                            }
                        },
                        scales: {
                            x: { grid: { display: false }, ticks: { color: textColor } },
                            y: { grid: { color: gridColor }, ticks: { color: textColor } }
                        }
                    }
                });
            }

            // 2. Category Doughnut Chart
            const ctxCat = document.getElementById('chartCategory');
            if (ctxCat && this.analytics.categories_breakdown) {
                if (this.chartCategory) this.chartCategory.destroy();

                const cats = this.analytics.categories_breakdown.slice(0, 8);
                const currKey = `total_${this.baseCurrency.toLowerCase()}`;

                this.chartCategory = new Chart(ctxCat, {
                    type: 'doughnut',
                    data: {
                        labels: cats.map(c => c.name),
                        datasets: [{
                            data: cats.map(c => c[currKey] || 0),
                            backgroundColor: cats.map(c => c.color || '#3b82f6'),
                            borderColor: '#0f172a',
                            borderWidth: 2
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                position: 'bottom',
                                labels: { color: textColor, font: { size: 11 }, boxWidth: 12 }
                            }
                        },
                        cutout: '70%'
                    }
                });
            }

            // 3. Currency Allocation Doughnut Chart
            const ctxAlloc = document.getElementById('chartAllocation');
            if (ctxAlloc && this.dashboard.currency_allocation) {
                if (this.chartAllocation) this.chartAllocation.destroy();
                const alloc = this.dashboard.currency_allocation;

                this.chartAllocation = new Chart(ctxAlloc, {
                    type: 'doughnut',
                    data: {
                        labels: ['Доллары ($)', 'Лиры (₺)', 'Тенге (₸)'],
                        datasets: [{
                            data: [alloc.USD || 0, alloc.TRY || 0, alloc.KZT || 0],
                            backgroundColor: ['#10b981', '#f43f5e', '#0ea5e9'],
                            borderColor: '#0f172a',
                            borderWidth: 2
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                position: 'bottom',
                                labels: { color: textColor, font: { size: 12 } }
                            }
                        },
                        cutout: '65%'
                    }
                });
            }
        }
    }));
});

function round(val, dec = 2) {
    return Math.round(val * Math.pow(10, dec)) / Math.pow(10, dec);
}
