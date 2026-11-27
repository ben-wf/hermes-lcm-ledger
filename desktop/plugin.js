import { gatewayActivationEpoch, host, useGatewayActivationEpoch, useValue } from '@hermes/plugin-sdk'
import { createElement as h, useEffect, useState } from 'react'

const pluginId = 'hermes-lcm-ledger'
const inputStyle = 'rounded border bg-background px-2 py-1 text-foreground'
const buttonStyle = 'rounded border px-3 py-1 disabled:opacity-50'

function display(field) {
  if (field.type === 'json') return JSON.stringify(field.value, null, 2)
  return String(field.value ?? '')
}

function parse(field, raw) {
  if (field.type === 'boolean') return raw === 'true'
  if (field.type === 'json') {
    const value = JSON.parse(raw)
    if (Array.isArray(field.default)) {
      if (!Array.isArray(value) || !value.every(item => typeof item === 'string')) throw Error('Enter a JSON list of strings.')
    } else if (!value || typeof value !== 'object' || Array.isArray(value) ||
        !Object.entries(value).every(([key, n]) => Object.hasOwn(field.default, key) && typeof n === 'number' && Number.isFinite(n) && n >= 0)) {
      throw Error('Enter an object with nonnegative weights for fts, summary or chunk.')
    }
    return value
  }
  if (field.type === 'number') {
    const n = Number(raw)
    if (raw.trim() === '' || !Number.isFinite(n) || (field.integer && !Number.isSafeInteger(n))) {
      throw Error(field.integer ? 'Enter a whole number.' : 'Enter a number.')
    }
    if (field.key === 'context_threshold' && (n <= 0 || n > 1)) throw Error('Enter a fraction greater than 0 and at most 1.')
    return n
  }
  return raw
}

function sourceLabel(source) {
  if (source === 'plugin_settings') return 'Ledger profile setting'
  if (source === 'default') return 'Ledger default'
  return source.replace(/^env:/, 'Environment · ').replace(/^config_yaml:/, 'Hermes · ')
}

function control(field, raw, change, disabled) {
  const common = {
    id: 'ledger-' + field.key, disabled, className: inputStyle,
    placeholder: field.automatic_path, value: raw, onChange: e => change(e.target.value)
  }
  if (field.type === 'boolean') return h('input', {
    id: common.id, disabled, type: 'checkbox', checked: raw === 'true',
    onChange: e => change(String(e.target.checked))
  })
  if (field.type === 'enum') return h('select', common, [...new Set([raw, ...field.choices])].map(
    item => h('option', {key: item, value: item}, item || '(automatic)')
  ))
  if (field.type === 'json') return h('textarea', {...common, rows: 3, spellCheck: false})
  return h('input', {...common, type: field.type === 'number' ? 'number' : 'text', step: field.integer ? 1 : 'any'})
}

const emptyAtom = {get: () => null, listen: () => () => {}}

function useSessionValues(profile, epoch, refresh) {
  const session = useValue(host.state.focusedSessionId || emptyAtom)
  const owner = useValue(host.state.focusedSessionOwner || emptyAtom)
  const connection = useValue(host.state.connectionId || emptyAtom)
  const [snapshot, setSnapshot] = useState(null)
  const target = session && owner?.profile === profile && owner.connectionId === (connection || 'local') ? session : null
  useEffect(() => {
    let active = true
    setSnapshot(null)
    if (target) {
      host.request('plugin.' + pluginId + '.session_settings', {session_id: target}).then(result => {
        if (active && result?.session_id === target) setSnapshot(result)
      }).catch(() => {})
    }
    return () => { active = false }
  }, [target, epoch, refresh])
  return target && snapshot?.session_id === target ? snapshot : null
}

function runtimeLabel(field, runtime) {
  if (runtime?.loaded && Object.hasOwn(runtime.loaded, field.key)) {
    const value = runtime.loaded[field.key]
    const pending = field.value !== value ? ' · differs from profile' : ''
    const effective = field.key === 'context_threshold' && value !== runtime.context_threshold
      ? ' · current model uses ' + runtime.context_threshold : ''
    return 'Loaded in focused chat: ' + String(value) + pending + effective
  }
  if (runtime?.matches && Object.hasOwn(runtime.matches, field.key)) {
    return runtime.matches[field.key] ? 'Loaded setting matches the gateway profile' : 'Loaded setting differs from the gateway profile'
  }
  return 'Read when Ledger initializes'
}

function Form({ctx, profile, epoch}) {
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState({})
  const [refresh, setRefresh] = useState(0)
  const [query, setQuery] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const runtime = useSessionValues(profile, epoch, refresh)
  const sameProfile = () => host.state.profile.get() === profile && gatewayActivationEpoch() === epoch

  useEffect(() => {
    let active = true
    setData(null)
    ctx.rest('/settings').then(result => {
      if (!active || !sameProfile()) return
      if (result.profile !== profile) throw Error('The settings service returned a different profile.')
      setData(result)
    }).catch(error => { if (active && sameProfile()) setMessage(error.message) })
    return () => { active = false }
  }, [ctx, profile, epoch, refresh])

  function edit(field, raw) {
    setDraft(previous => {
      const next = {...previous}
      if (raw === display(field)) delete next[field.key]
      else next[field.key] = raw
      return next
    })
    setMessage('')
  }

  async function save() {
    if (!data || !sameProfile() || busy) return
    setBusy(true)
    try {
      const values = {}
      for (const field of data.fields) {
        if (!Object.hasOwn(draft, field.key)) continue
        try {
          const value = parse(field, draft[field.key])
          if (JSON.stringify(value) !== JSON.stringify(field.value)) values[field.key] = value
        } catch (error) { throw Error(field.label + ' — ' + error.message) }
      }
      if (Object.keys(values).length) {
        const params = {action: 'settings', key: pluginId, values}
        if (profile !== 'custom') params.profile = profile
        const result = await host.request('plugins.manage', params)
        if (!result?.ok) throw Error('Hermes could not save the settings.')
      }
      if (!sameProfile()) return
      setDraft({})
      setMessage('Saved to the profile. Restart the Hermes process serving this profile to load changes.')
      setRefresh(n => n + 1)
    } catch (error) { if (sameProfile()) setMessage(error.message) }
    finally { if (sameProfile()) setBusy(false) }
  }

  const dirty = Object.keys(draft).length > 0
  const filter = query.trim().toLowerCase()
  const fields = data?.fields.filter(f => [f.key, f.label, f.source].join(' ').toLowerCase().includes(filter)) ?? []
  return h('div', {className: 'grid gap-4'}, [
    h('h2', {key: 'title', className: 'text-lg font-semibold'}, 'LCM Ledger · ' + profile),
    h('p', {key: 'scope'}, runtime
      ? 'Inputs show resolved profile settings. Loaded values refer to the focused chat. Startup-only options show their configured value.'
      : 'Inputs show resolved profile settings. Live settings for a focused Ledger chat are unavailable.'),
    h('div', {key: 'actions', className: 'flex flex-wrap gap-2'}, [
      h('input', {key: 'filter', className: inputStyle, type: 'search', placeholder: 'Find a setting', 'aria-label': 'Find a setting', value: query, onChange: e => setQuery(e.target.value)}),
      h('button', {key: 'reload', className: buttonStyle, disabled: busy || dirty, onClick: () => {setMessage(''); setRefresh(n => n + 1)}}, 'Refresh'),
      h('button', {key: 'save', className: buttonStyle, disabled: busy || !dirty, onClick: save}, 'Save changes')
    ]),
    message && h('p', {key: 'message', role: 'status'}, message),
    !data && !message && h('p', {key: 'loading', role: 'status'}, 'Loading profile settings…'),
    data && !fields.length && h('p', {key: 'empty'}, 'No matching settings.'),
    ...fields.map(field => h('div', {key: field.key, className: 'grid gap-1 border-b py-2'}, [
      h('label', {key: 'label', htmlFor: 'ledger-' + field.key, className: 'font-medium'}, field.label),
      h('p', {key: 'desc', className: 'text-sm text-muted-foreground'}, field.description),
      h('div', {key: 'control'}, control(field, draft[field.key] ?? display(field), raw => edit(field, raw), busy)),
      h('small', {key: 'source'}, sourceLabel(field.source)),
      runtime && h('small', {key: 'runtime'}, runtimeLabel(field, runtime))
    ]))
  ])
}

function Settings({ctx}) {
  const profile = useValue(host.state.profile)
  const epoch = useGatewayActivationEpoch()
  return h(Form, {key: JSON.stringify([epoch, profile]), ctx, profile, epoch})
}

export default {
  id: pluginId,
  name: 'LCM Ledger',
  register(ctx) {
    ctx.registerSettingsPage({id: 'profile', title: 'LCM Ledger', render: () => h(Settings, {ctx})})
  }
}
