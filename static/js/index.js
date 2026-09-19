window.app = Vue.createApp({
  el: '#vue',
  mixins: [windowMixin],
  data() {
    return {
      activeTab: 'plans',
      selectedWallet: null,
      plans: [],
      loadingPlans: false,
      historyLogs: [],
      loadingHistory: false,
      availableCurrencies: [{ code: 'SAT', name: 'Satoshis' }],

      // Table columns
      planColumns: [
        { name: 'is_active', label: 'Active', field: 'is_active', align: 'center' },
        { name: 'name', label: 'Plan Name', field: 'name', align: 'left', sortable: true },
        { name: 'cadence', label: 'Schedule', align: 'left' },
        { name: 'next_run_at', label: 'Next Run', field: 'next_run_at', align: 'left', sortable: true },
        { name: 'recipients', label: 'Recipients & Budget', align: 'left' },
        { name: 'actions', label: 'Actions', align: 'right' },
      ],
      historyColumns: [
        { name: 'executed_at', label: 'Timestamp', field: 'executed_at', align: 'left', sortable: true },
        { name: 'status', label: 'Status', field: 'status', align: 'center' },
        { name: 'triggered_by', label: 'Trigger', field: 'triggered_by', align: 'center' },
        { name: 'total_sats', label: 'Total Debited', field: 'total_sats', align: 'right', sortable: true },
        { name: 'details', label: 'Breakdown', align: 'right' },
      ],

      // Dialog: Create/Edit Plan
      planDialog: {
        show: false,
        saving: false,
        advancedCron: false,
        presetCadence: 'weekly',
        presetWeekday: '5', // Friday
        presetMonthDay: 1,
        presetTime: '09:00',
        data: {
          id: null,
          name: '',
          description: '',
          cadence_type: 'weekly',
          cron_expression: '0 9 * * 5',
          timezone: 'UTC',
          max_sat_limit: null,
          low_balance_threshold: 0,
          telegram_chat_id: null,
          items: [],
        },
      },

      // Dialog: Simulation
      simDialog: {
        show: false,
        data: null,
      },

      // Dialog: Execution details breakdown
      detailsDialog: {
        show: false,
        data: null,
      },

      // Settings form
      settingsForm: {
        telegram_bot_token: '',
        telegram_chat_id: '',
        notify_on_success: true,
        notify_on_failure: true,
        notify_on_low_balance: true,
      },
      savingSettings: false,
      testingTelegram: false,
      g: (typeof window !== 'undefined' && window.g) ? window.g : { user: null },
    }
  },

  computed: {
    activeWalletAdminKey() {
      const user = (this.g && this.g.user) || (typeof window !== 'undefined' && window.g && window.g.user)
      if (!this.selectedWallet || !user || !user.wallets) return null
      const w = user.wallets.find(x => x.id === this.selectedWallet)
      return w ? w.adminkey : null
    },

    userWallets() {
      const user = (this.g && this.g.user) || (typeof window !== 'undefined' && window.g && window.g.user)
      return (user && user.wallets) || []
    },

    internalWalletOptions() {
      return this.userWallets
        .filter(w => w.id !== this.selectedWallet)
        .map(w => ({
          label: `${w.name} (${(w.balance_msat ? Math.floor(w.balance_msat / 1000) : 0).toLocaleString()} sats)`,
          value: w.id
        }))
    },

    locationOrigin() {
      return (typeof window !== 'undefined' && window.location) ? window.location.origin : ''
    },
  },

  methods: {
    copyCurl(cmd) {
      LNbits.utils.copyText(cmd, 'Curl command copied to clipboard!')
    },

    onWalletChange() {
      this.fetchPlans()
      this.fetchHistory()
      this.fetchSettings()
    },

    async fetchCurrencies() {
      try {
        const { data } = await LNbits.api.request(
          'GET',
          '/pocketmoney/api/v1/currencies',
        )
        this.availableCurrencies = data
      } catch (err) {
        console.error('Failed to load currencies:', err)
      }
    },

    async fetchPlans() {
      if (!this.activeWalletAdminKey) return
      this.loadingPlans = true
      try {
        const { data } = await LNbits.api.request(
          'GET',
          '/pocketmoney/api/v1/plans',
          this.activeWalletAdminKey,
        )
        this.plans = data
      } catch (err) {
        LNbits.utils.notifyApiError(err)
      } finally {
        this.loadingPlans = false
      }
    },

    async fetchHistory() {
      if (!this.activeWalletAdminKey) return
      this.loadingHistory = true
      try {
        const { data } = await LNbits.api.request(
          'GET',
          '/pocketmoney/api/v1/history',
          this.activeWalletAdminKey,
        )
        this.historyLogs = data
      } catch (err) {
        LNbits.utils.notifyApiError(err)
      } finally {
        this.loadingHistory = false
      }
    },

    async fetchSettings() {
      if (!this.activeWalletAdminKey) return
      try {
        const { data } = await LNbits.api.request(
          'GET',
          '/pocketmoney/api/v1/settings',
          this.activeWalletAdminKey,
        )
        this.settingsForm = {
          telegram_bot_token: data.telegram_bot_token || '',
          telegram_chat_id: data.telegram_chat_id || '',
          notify_on_success: data.notify_on_success !== false,
          notify_on_failure: data.notify_on_failure !== false,
          notify_on_low_balance: data.notify_on_low_balance !== false,
        }
      } catch (err) {
        console.error('Error fetching settings:', err)
      }
    },

    async saveSettings() {
      if (!this.activeWalletAdminKey) return
      this.savingSettings = true
      try {
        await LNbits.api.request(
          'PUT',
          '/pocketmoney/api/v1/settings',
          this.activeWalletAdminKey,
          this.settingsForm,
        )
        Quasar.Notify.create({
          type: 'positive',
          message: 'PocketMoney settings saved successfully!',
        })
      } catch (err) {
        LNbits.utils.notifyApiError(err)
      } finally {
        this.savingSettings = false
      }
    },

    async testTelegram() {
      if (!this.activeWalletAdminKey) return
      if (!this.settingsForm.telegram_bot_token || !this.settingsForm.telegram_chat_id) {
        Quasar.Notify.create({
          type: 'warning',
          message: 'Please fill in both the Bot Token and Chat ID to send a test message.',
        })
        return
      }
      this.testingTelegram = true
      try {
        await LNbits.api.request(
          'POST',
          '/pocketmoney/api/v1/settings/test-telegram',
          this.activeWalletAdminKey,
          {
            telegram_bot_token: this.settingsForm.telegram_bot_token,
            telegram_chat_id: this.settingsForm.telegram_chat_id,
          },
        )
        Quasar.Notify.create({
          type: 'positive',
          message: 'Test Telegram message sent successfully! Check your Telegram.',
        })
      } catch (err) {
        LNbits.utils.notifyApiError(err)
      } finally {
        this.testingTelegram = false
      }
    },

    openPlanDialog(plan = null) {
      if (plan) {
        this.planDialog.data = {
          id: plan.id,
          name: plan.name,
          description: plan.description || '',
          cadence_type: plan.cadence_type,
          cron_expression: plan.cron_expression,
          timezone: plan.timezone || 'UTC',
          max_sat_limit: plan.max_sat_limit,
          low_balance_threshold: plan.low_balance_threshold || 0,
          telegram_chat_id: plan.telegram_chat_id,
          items: (plan.items || []).map(i => {
            const isInternal = (this.g.user && this.g.user.wallets || []).some(w => w.id === i.recipient)
            return {
              label: i.label,
              recipient: i.recipient,
              recipientMode: isInternal ? 'wallet' : 'manual',
              amount: i.amount,
              currency: i.currency,
              memo: i.memo || '',
              max_sat_limit: i.max_sat_limit,
            }
          }),
        }
        this.planDialog.advancedCron = plan.cadence_type === 'cron'
      } else {
        this.planDialog.data = {
          id: null,
          name: '',
          description: '',
          cadence_type: 'weekly',
          cron_expression: '0 9 * * 5',
          timezone: 'UTC',
          max_sat_limit: null,
          low_balance_threshold: 0,
          telegram_chat_id: null,
          items: [
            { label: '', recipient: '', recipientMode: 'wallet', amount: 4, currency: 'EUR', memo: '' },
          ],
        }
        this.planDialog.advancedCron = false
        this.planDialog.presetCadence = 'weekly'
        this.planDialog.presetWeekday = '5'
        this.planDialog.presetTime = '09:00'
      }
      this.planDialog.show = true
    },

    addRecipientRow() {
      this.planDialog.data.items.push({
        label: '',
        recipient: '',
        recipientMode: 'wallet',
        amount: 1000,
        currency: 'SAT',
        memo: '',
        max_sat_limit: null,
      })
    },

    onRecipientModeChange(item) {
      item.recipient = ''
    },

    onInternalWalletSelected(item, walletId) {
      if (!walletId || !this.g.user || !this.g.user.wallets) return
      const w = this.g.user.wallets.find(x => x.id === walletId)
      if (w && (!item.label || item.label.trim() === '')) {
        item.label = w.name
      }
    },

    removeRecipientRow(idx) {
      this.planDialog.data.items.splice(idx, 1)
    },

    updateCronFromPresets() {
      if (this.planDialog.advancedCron) return
      const [hh, mm] = (this.planDialog.presetTime || '09:00').split(':')
      const m = parseInt(mm || '0', 10)
      const h = parseInt(hh || '9', 10)

      if (this.planDialog.presetCadence === 'daily') {
        this.planDialog.data.cron_expression = `${m} ${h} * * *`
        this.planDialog.data.cadence_type = 'daily'
      } else if (this.planDialog.presetCadence === 'weekly') {
        const w = this.planDialog.presetWeekday || '5'
        this.planDialog.data.cron_expression = `${m} ${h} * * ${w}`
        this.planDialog.data.cadence_type = 'weekly'
      } else if (this.planDialog.presetCadence === 'monthly') {
        const d = this.planDialog.presetMonthDay || 1
        this.planDialog.data.cron_expression = `${m} ${h} ${d} * *`
        this.planDialog.data.cadence_type = 'monthly'
      }
    },

    async submitPlanForm() {
      if (!this.activeWalletAdminKey) return
      if (!this.planDialog.data.name) return

      this.planDialog.saving = true
      try {
        if (!this.planDialog.advancedCron) {
          this.updateCronFromPresets()
        } else {
          this.planDialog.data.cadence_type = 'cron'
        }

        if (this.planDialog.data.id) {
          await LNbits.api.request(
            'PUT',
            `/pocketmoney/api/v1/plans/${this.planDialog.data.id}`,
            this.activeWalletAdminKey,
            this.planDialog.data,
          )
          Quasar.Notify.create({ type: 'positive', message: 'Plan updated successfully!' })
        } else {
          await LNbits.api.request(
            'POST',
            '/pocketmoney/api/v1/plans',
            this.activeWalletAdminKey,
            this.planDialog.data,
          )
          Quasar.Notify.create({ type: 'positive', message: 'Plan created successfully!' })
        }
        this.planDialog.show = false
        await this.fetchPlans()
      } catch (err) {
        LNbits.utils.notifyApiError(err)
      } finally {
        this.planDialog.saving = false
      }
    },

    async togglePlanActive(plan) {
      if (!this.activeWalletAdminKey) return
      try {
        await LNbits.api.request(
          'PUT',
          `/pocketmoney/api/v1/plans/${plan.id}`,
          this.activeWalletAdminKey,
          { is_active: plan.is_active },
        )
        Quasar.Notify.create({
          type: 'info',
          message: `Plan ${plan.name} is now ${plan.is_active ? 'Active' : 'Paused'}.`,
        })
      } catch (err) {
        LNbits.utils.notifyApiError(err)
        plan.is_active = !plan.is_active
      }
    },

    runPlanNow(plan) {
      Quasar.Dialog.create({
        title: 'Execute Allowance Plan Now?',
        message: `Are you sure you want to trigger '${plan.name}' immediately? This will debit your wallet and transfer funds to all ${plan.items.length} recipient(s).`,
        cancel: true,
        persistent: true,
        ok: { label: 'Yes, Pay Now', color: 'positive', unelevated: true },
      }).onOk(async () => {
        try {
          Quasar.Loading.show({ message: `Executing ${plan.name}...` })
          const { data } = await LNbits.api.request(
            'POST',
            `/pocketmoney/api/v1/plans/${plan.id}/run`,
            this.activeWalletAdminKey,
          )
          if (data.status === 'success') {
            Quasar.Notify.create({
              type: 'positive',
              message: `Successfully executed '${plan.name}'! Total: ${data.total_sats.toLocaleString()} sats.`,
            })
          } else {
            Quasar.Notify.create({
              type: 'warning',
              message: `Plan execution status: ${data.status.toUpperCase()}. Reason: ${data.error_message || 'Check logs'}`,
            })
          }
          await this.fetchPlans()
          await this.fetchHistory()
        } catch (err) {
          LNbits.utils.notifyApiError(err)
        } finally {
          Quasar.Loading.hide()
        }
      })
    },

    async simulatePlan(plan) {
      if (!this.activeWalletAdminKey) return
      try {
        Quasar.Loading.show({ message: 'Running dry-run simulation...' })
        const { data } = await LNbits.api.request(
          'POST',
          `/pocketmoney/api/v1/plans/${plan.id}/simulate`,
          this.activeWalletAdminKey,
        )
        this.simDialog.data = data
        this.simDialog.show = true
      } catch (err) {
        LNbits.utils.notifyApiError(err)
      } finally {
        Quasar.Loading.hide()
      }
    },

    copyWebhookUrl(plan) {
      const url = `${window.location.origin}/pocketmoney/api/v1/webhook/${plan.webhook_token}`
      LNbits.utils.copyText(url, 'Webhook trigger URL copied to clipboard!')
    },

    deletePlan(plan) {
      Quasar.Dialog.create({
        title: 'Delete Plan?',
        message: `Permanently delete '${plan.name}'? This cannot be undone.`,
        cancel: true,
        persistent: true,
        ok: { label: 'Delete', color: 'negative', flat: true },
      }).onOk(async () => {
        try {
          await LNbits.api.request(
            'DELETE',
            `/pocketmoney/api/v1/plans/${plan.id}`,
            this.activeWalletAdminKey,
          )
          Quasar.Notify.create({ type: 'positive', message: 'Plan deleted.' })
          await this.fetchPlans()
        } catch (err) {
          LNbits.utils.notifyApiError(err)
        }
      })
    },

    openDetailsDialog(log) {
      this.detailsDialog.data = log
      this.detailsDialog.show = true
    },

    formatCadence(plan) {
      if (plan.cadence_type === 'cron') return `Cron: ${plan.cron_expression}`
      const [m, h, d, mo, w] = plan.cron_expression.split(' ')
      const timeStr = `${h.padStart(2, '0')}:${m.padStart(2, '0')}`
      if (plan.cadence_type === 'daily') return `Daily at ${timeStr}`
      if (plan.cadence_type === 'weekly') {
        const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
        const dayName = days[parseInt(w, 10)] || 'Fri'
        return `Weekly (${dayName} ${timeStr})`
      }
      if (plan.cadence_type === 'monthly') {
        return `Monthly (Day ${d} at ${timeStr})`
      }
      return plan.cron_expression
    },

    formatDateTime(dtStr) {
      if (!dtStr) return 'N/A'
      return Quasar.date.formatDate(new Date(dtStr), 'YYYY-MM-DD HH:mm')
    },

    formatRelativeTime(dtStr) {
      if (!dtStr) return ''
      const target = new Date(dtStr).getTime()
      const now = Date.now()
      const diffMs = target - now
      if (diffMs <= 0) return 'Due now'
      const mins = Math.floor(diffMs / 60000)
      if (mins < 60) return `in ${mins} min(s)`
      const hours = Math.floor(mins / 60)
      if (hours < 24) return `in ${hours} hour(s)`
      const days = Math.floor(hours / 24)
      return `in ${days} day(s)`
    },

    formatItemsSummary(items) {
      if (!items || !items.length) return 'No items'
      return items.map(i => `${i.label} (${i.amount} ${i.currency})`).join(', ')
    },
  },

  created() {
    if (this.g.user && this.g.user.wallets && this.g.user.wallets.length) {
      this.selectedWallet = this.g.user.wallets[0].id
    }
    this.fetchCurrencies()
    this.fetchPlans()
    this.fetchHistory()
    this.fetchSettings()
  },
})
