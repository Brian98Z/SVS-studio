import { WORKLET_PROCESSOR_NAME, workletProcessorSource } from './fx.js'
import { NATIVE_MIXER_PROCESSOR_NAME, nativeMixerProcessorSource } from './native-mixer.js'
import { apiUrl } from './origin.js'
const REQUEST_CONCURRENCY = 2
const PREFETCH_WINDOW_MS = 6000
const SCHEDULE_AHEAD_MS = 350
const MAX_CACHE_BYTES = 64 * 1024 * 1024
const PHRASE_TAIL_MS = 250
function browserEffects(effects) { return (effects || []).filter(e => e.type !== 'vst3') }

function enabledVstEffects(effects) { return Array.isArray(effects) && effects.some(effect => effect?.type === 'vst3' && effect.enabled === true) }
function projectHasEnabledVst(project) {
  if (enabledVstEffects(project?.mixer?.master?.effects)) return true
  if ((project?.mixer?.buses || []).some(bus => enabledVstEffects(bus?.effects))) return true
  return (project?.tracks || []).some(track => enabledVstEffects(track?.fx || track?.effects))
}
export class PlaybackEngine {
  constructor(store, api, tickToMs, msToTick) {
    this.store = store
    this.api = api
    this.tickToMs = tickToMs
    this.msToTick = msToTick
    this.context = null
    this.master = null
    this.workletUrl = null
    this.workletReady = null
    this.masterFx = null
    this.busNodes = new Map()
    this.trackNodes = new Map()
    this.plan = null
    this.buffers = new Map()
    this.bufferBytes = 0
    this.requests = new Map()
    this.sources = new Map()
    this.completed = new Set()
    this.generation = 0
    this.frameId = 0
    this.anchorMs = 0
    this.anchorTime = 0
    this.heldMs = 0
    this.clockRunning = false
    this.timelineEndMs = 0
    this.destroyed = false
    this.nativeSessionId = null
    this.nativeNode = null
    this.deviceOutputId = null
    this.deviceClosePromise = Promise.resolve()
    this.deviceOpenPromise = Promise.resolve()
    this.nativeStreamWorker = null
    this.nativeUpdatePending = false
    this.nativeStats = { latency_ms: 0, underruns: 0, processed_blocks: 0, processing_ms: 0 }
    this.nativeBlockFrames = 2048
    this.nativeTargetBlocks = 4
    this.nativeTailMs = 0
    this.nativeLatencySamples = 0
    this.nativeEndMs = 0
  }

  nativeConfiguration() {
    const savedTracks = this.store.project.tracks || []
    const tracks = (this.plan?.tracks || []).map(t => {
      const saved = savedTracks.find(x => x.id === t.id) || t
      return { id: t.id, volume: saved.volume ?? t.volume ?? 1, pan: 0, bus_gain: t.bus_gain ?? saved.bus_gain ?? 1,
        mute: !!saved.mute, solo: !!saved.solo, fx: saved.fx || saved.effects || [], sends: saved.sends || [], bus: saved.bus || 'master' }
    })
    const buses = (this.store.project.mixer?.buses || []).map(b => ({ ...b, effects: b.effects || [], sends: b.sends || [] }))
    const master = this.store.project.mixer?.master || {}
    const mixer = { master: { ...master, volume: this.store.project.master_volume ?? master.volume ?? 1, effects: master.effects || [] }, buses }
    return { tracks, mixer }
  }

  async openNativeSession(generation) {
    if (!this.isCurrent(generation)) return
    const { block_frames = 2048, target_blocks = 4 } = this.store.audition || {}
    if (![512, 1024, 2048, 4096].includes(block_frames) || ![2, 4].includes(target_blocks)) {
      throw new Error('试听分块或预缓冲设置无效；请停止后重新选择。')
    }
    this.nativeBlockFrames = block_frames
    this.nativeTargetBlocks = target_blocks
    const { tracks, mixer } = this.nativeConfiguration()
    if (!tracks.length || tracks.length > 32) throw new Error('原生混音器轨道数无效（最多 32 条）')
    const response = await fetch(apiUrl('/api/inference/vst/realtime/open'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sample_rate: this.context.sampleRate, block_frames: this.nativeBlockFrames, tracks, mixer }) })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.detail || data.error || response.statusText)
    if (!this.isCurrent(generation)) { this.closeNativeId(data.session_id); return }
    this.nativeSessionId = data.session_id
    this.store.transport.native_session_id = data.session_id
    this.nativeLatencySamples = data.latency_samples || 0
    this.nativeTrackIds = tracks.map(t => t.id)
    this.nativeEndMs = Math.max(this.timelineEndMs, ...this.plan.phrases.map(p => p.end_ms || 0))
    this.nativeTailMs = this.requestedNativeTail(tracks, mixer)
    this.nativeEndMs += this.nativeTailMs
    this.nativeStats = { latency_ms: this.nativeTargetBlocks * this.nativeBlockFrames * 1000 / this.context.sampleRate,
      plugin_latency_ms: this.nativeLatencySamples * 1000 / this.context.sampleRate, underruns: 0, processed_blocks: 0, processing_ms: 0 }
    this.publishNativeStats()
    const deviceOutput = this.store.transport.output === 'device'
    let output = null
    if (deviceOutput) {
      this.deviceOpenPromise = (async () => {
        const opened = await fetch(apiUrl('/api/audio/output/open'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sample_rate: this.context.sampleRate, block_frames: this.nativeBlockFrames, target_blocks: this.nativeTargetBlocks, device_id: this.store.transport.output_device_id || null }) })
        output = await opened.json().catch(() => ({}))
        if (!opened.ok) throw new Error(output.detail || output.error || '无法打开原生声卡')
        if (!this.isCurrent(generation)) { await fetch(apiUrl(`/api/audio/output/${output.id}`), { method: 'DELETE' }); return }
        this.deviceOutputId = output.id
        this.store.transport.device_output_id = output.id
      })()
      await this.deviceOpenPromise
      if (!this.isCurrent(generation)) return
      this.nativeStats.device = output.device
      this.nativeStats.output = 'device'
    } else this.nativeStats.output = 'browser'
    this.publishNativeStats()
    this.nativeNode = new AudioWorkletNode(this.context, NATIVE_MIXER_PROCESSOR_NAME, {
      numberOfInputs: tracks.length, numberOfOutputs: 1, outputChannelCount: [2], channelCount: 2,
      channelCountMode: 'explicit', processorOptions: { tracks: this.nativeTrackIds, blockFrames: this.nativeBlockFrames, targetBlocks: this.nativeTargetBlocks, captureOnly: deviceOutput },
    })
    this.nativeNode.connect(this.context.destination)
    const channel = new MessageChannel()
    this.nativeStreamWorker = new Worker(new URL('./native-stream-worker.js', import.meta.url), { type: 'module' })
    this.nativeStreamWorker.onerror = event => { if (this.isCurrent(generation)) this.fail(new Error(event.message || '原生混音流线程失败')) }
    this.nativeStreamWorker.onmessage = ({ data }) => {
      if (!this.isCurrent(generation) || !this.nativeSessionId) return
      if (data.type === 'clock') { this.nativeHeardFrames = data.frames; this.update(generation) }
      else if (data.type === 'stats') { Object.assign(this.nativeStats, data.stats); this.publishNativeStats() }
      else if (data.type === 'error') this.fail(new Error(data.message))
    }
    this.nativeNode.port.postMessage({ type: 'stream', port: channel.port1 }, [channel.port1])
    this.nativeStreamWorker.postMessage({ type: 'init', port: channel.port2, sessionId: data.session_id,
      endpoint: new URL(apiUrl('/api/inference/vst/realtime/process'), location.href).href,
      frames: this.nativeBlockFrames, rate: this.context.sampleRate, targetBlocks: this.nativeTargetBlocks,
      trackIds: this.nativeTrackIds, deviceOutput: output ? { id: output.id, endpoint: new URL(apiUrl(`/api/audio/output/${output.id}`), location.href).href } : null }, [channel.port2])
    this.nativeNode.port.postMessage({ type: 'flush' })
    this.nativeNode.underrunBlocks = 0
    for (const n of this.trackNodes.values()) { n.fx.disconnect(); for (const x of n.routeNodes || []) try { x.disconnect() } catch {} }
    for (const n of this.busNodes.values()) { n.output.disconnect(); for (const x of n.routeNodes || []) try { x.disconnect() } catch {} }
    for (let i = 0; i < tracks.length; i++) this.trackNodes.get(tracks[i].id)?.gain.connect(this.nativeNode, 0, i)
    this.nativeUpdating = false
    this.nativeTopology = this.nativeTopologySignature(tracks, mixer)
  }

  requestedNativeTail(tracks, mixer) {
    const chainTail = effects => (effects || []).reduce((sum, effect) =>
      sum + (effect.enabled ? Math.max(0, Number(effect.tail_seconds) || 0) : 0), 0)
    const buses = new Map((mixer.buses || []).map(bus => [bus.id, bus]))
    const memo = new Map([['master', chainTail(mixer.master?.effects)]])
    const visiting = new Set()
    const downstream = id => {
      if (memo.has(id)) return memo.get(id)
      const bus = buses.get(id)
      if (!bus || visiting.has(id)) throw new Error('混音总线路由无效或存在循环')
      visiting.add(id)
      const destinations = [bus.output || 'master', ...(bus.sends || []).map(send => send.to)]
      const seconds = chainTail(bus.effects) + Math.max(...destinations.map(downstream))
      visiting.delete(id)
      memo.set(id, seconds)
      return seconds
    }
    return Math.max(0, ...tracks.map(track => chainTail(track.fx) + Math.max(
      ...[track.bus || 'master', ...(track.sends || []).map(send => send.to)].map(downstream)))) * 1000
  }


  publishNativeStats() {
    if (!this.nativeMode || !this.store.transport) return
    this.store.transport.native = { ...this.nativeStats }
  }

  closeNativeId(id) {
    if (!id) return
    fetch(apiUrl('/api/inference/vst/realtime/close'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: id }), keepalive: true }).catch(() => {})
  }

  closeNativeSession() {
    const id = this.nativeSessionId
    this.nativeSessionId = null
    this.store.transport.native_session_id = null
    this.nativeStreamWorker?.terminate()
    this.nativeStreamWorker = null
    const deviceId = this.deviceOutputId
    this.deviceOutputId = null
    this.store.transport.device_output_id = null
    if (deviceId) this.deviceClosePromise = this.deviceClosePromise.then(async () => {
      const response = await fetch(apiUrl(`/api/audio/output/${deviceId}`), { method: 'DELETE', keepalive: true })
      if (!response.ok) throw new Error('原生声卡关闭失败')
    }).catch(error => { this.store.status = error.message })
    if (this.nativeNode) {
      this.nativeNode.port.postMessage({ type: 'flush' })
      try { this.nativeNode.disconnect() } catch {}
      for (const n of this.trackNodes.values()) try { n.gain.disconnect(this.nativeNode) } catch {}
      this.nativeNode.port.onmessage = null
      this.nativeNode = null
    }
    this.closeNativeId(id)
  }
  positionMs() {
    if (!this.clockRunning) return this.heldMs
    if (this.nativeMode) return this.anchorMs + Math.max(0, (this.nativeHeardFrames || 0) - this.nativeLatencySamples) * 1000 / this.context.sampleRate
    return Math.max(this.heldMs, this.anchorMs + (this.context.currentTime - this.anchorTime) * 1000)
  }

  isCurrent(generation) {
    return !this.destroyed && generation === this.generation
  }
  contextForGesture() {
    const requestedRate = this.store.transport.output === 'device' ? this.store.transport.output_sample_rate : null
    if (this.context && requestedRate && this.context.sampleRate !== requestedRate) {
      this.projectChanged()
      this.masterFx?.disconnect()
      this.master?.disconnect()
      this.masterVolume?.disconnect()
      this.context.close().catch(error => { this.store.status = error.message })
      this.context = null
      this.masterFx = null
      this.masterFxConnected = false
      this.masterFxSignature = null
      if (this.workletUrl) URL.revokeObjectURL(this.workletUrl)
    }
    if (!this.context) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext
      if (!AudioContextClass) throw new Error('此浏览器不支持 Web Audio')
      this.context = new AudioContextClass(requestedRate ? { sampleRate: requestedRate } : undefined)
      this.workletUrl = URL.createObjectURL(new Blob([workletProcessorSource], { type: 'text/javascript' }))
      this.workletReady = Promise.all([this.context.audioWorklet.addModule(this.workletUrl), this.context.audioWorklet.addModule(URL.createObjectURL(new Blob([nativeMixerProcessorSource], { type: 'text/javascript' })))])
      this.master = this.context.createGain()
      this.masterVolume = this.context.createGain()
      this.master.connect(this.masterVolume)
    }
    return this.context
  }

  play(tick = this.store.transport.tick) {
    let resume
    try {
      const context = this.contextForGesture()
      resume = context.resume()
    } catch (error) {
      this.fail(error)
      return Promise.resolve()
    }
    return this.startAt(tick, resume, this.store.transport.output === 'device' || projectHasEnabledVst(this.store.project))
  }

  seek(tick) {
    return this.play(tick)
  }

  async startAt(tick, resumePromise, native = false) {
    const generation = ++this.generation
    this.closeNativeSession()
    this.cancelRequests()
    this.stopSources()
    this.completed.clear()
    this.cancelFrame()
    this.heldMs = Math.max(0, this.tickToMs(this.store.project.tempos, tick))
    this.anchorMs = this.heldMs
    this.anchorTime = this.context.currentTime
    this.clockRunning = false
    this.nativeMode = native
    for (const n of this.trackNodes.values()) { n.gain.disconnect(); n.fx.disconnect(); for (const tap of n.routeNodes || []) tap.disconnect() }
    for (const n of this.busNodes.values()) { n.input.disconnect(); n.level.disconnect(); n.fx.disconnect(); n.output.disconnect(); for (const tap of n.routeNodes || []) tap.disconnect() }
    this.trackNodes.clear(); this.busNodes.clear()
    this.store.transport.tick = Math.max(0, tick)
    this.store.transport.ms = this.heldMs
    this.store.transport.playing = true
    this.store.transport.state = 'buffering'
    this.store.transport.pending = this.plan ? 0 : 1
    this.store.transport.error = ''
    this.store.transport.native = native ? { ...this.nativeStats } : null
    this.store.status = this.plan ? '试唱缓冲中…' : '正在读取试唱计划…'
    try {
      await Promise.all([resumePromise, this.workletReady, this.deviceClosePromise, this.deviceOpenPromise.catch(() => {})])
      if (!this.isCurrent(generation)) return
      if (!native) {
        this.masterFx = this.masterFx || new AudioWorkletNode(this.context, WORKLET_PROCESSOR_NAME, { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2], channelCount: 2, channelCountMode: 'explicit' })
        if (!this.masterFxConnected) { this.masterVolume.connect(this.masterFx); this.masterFx.connect(this.context.destination); this.masterFxConnected = true }
        const fx = browserEffects(this.store.project.mixer?.master?.effects)
        if (this.masterFxSignature !== JSON.stringify(fx)) { this.masterFx.port.postMessage({ type: 'effects', effects: JSON.parse(JSON.stringify(fx)) }); this.masterFxSignature = JSON.stringify(fx) }
      }
      if (!this.plan) {
        const controller = new AbortController()
        this.planController = controller
        const response = await fetch(apiUrl('/api/playback/plan'), { signal: controller.signal })
        const data = await response.json().catch(() => ({}))
        if (!response.ok) throw new Error(data.detail || data.error || response.statusText)
        if (!this.isCurrent(generation)) return
        this.plan = data
        this.planController = null
      }
      if (!this.isCurrent(generation)) return
      const plan = this.plan
      this.timelineEndMs = Math.max(this.timelineEndMs, plan.end_ms)
      const startMs = Math.max(0, Math.min(this.tickToMs(plan.tempos, tick), plan.end_ms))
      this.store.transport.tick = Math.max(0, Math.min(tick, plan.end_tick))
      this.store.transport.ms = startMs
      this.heldMs = startMs
      this.anchorMs = startMs
      this.anchorTime = this.context.currentTime + 0.06
      this.ensureTrackNodes()
      if (!native) {
        for (const [id, n] of this.trackNodes) { n.gain.disconnect(); n.gain.connect(n.fx); this.routeTrack(n, plan.tracks.find(t => t.id === id) || {}) }
        for (const n of this.busNodes.values()) n.routeSignature = null
        this.ensureTrackNodes()
      }
      if (!plan.phrases.length || startMs >= plan.end_ms) { this.finishAtEnd(); return }
      if (native) await this.openNativeSession(generation)
      if (!this.isCurrent(generation)) return
      this.clockRunning = false
      this.store.transport.state = 'buffering'
      this.scheduleFrame(generation)
      this.update(generation)
    } catch (error) {
      if (error?.name === 'AbortError' || !this.isCurrent(generation)) return
      this.fail(error)
    }
  }


  pause() {
    if (!this.store.transport.playing && this.store.transport.state !== 'buffering') return
    const position = this.positionMs()
    this.cancelSession('paused', position)
  }

  stop() {
    const position = this.positionMs()
    this.cancelSession('idle', position)
  }

  remote(action, tick) {
    if (action === 'play' || action === 'seek') {
      this.play(tick ?? this.store.transport.tick)
    } else if (action === 'stop') {
      const position = Number.isFinite(tick) ? this.tickToMs(this.plan?.tempos || this.store.project.tempos, tick) : this.positionMs()
      this.cancelSession('idle', Math.max(0, position), tick)
    }
  }

  projectChanged() {
    this.cancelSession('idle', this.positionMs())
    this.plan = null
    this.buffers.clear()
    this.completed.clear()
    this.bufferBytes = 0
    for (const nodes of this.trackNodes.values()) {
      nodes.gain.disconnect(); nodes.fx.disconnect()
      for (const extra of nodes.routeNodes || []) extra.disconnect()
    }
    for (const n of this.busNodes.values()) {
      n.input.disconnect(); n.level.disconnect(); n.fx.disconnect(); n.output.disconnect()
      for (const extra of n.routeNodes || []) extra.disconnect()
    }
    this.trackNodes.clear(); this.busNodes.clear()
    this.timelineEndMs = 0
    this.store.status = '工程已更改，试唱已停止'
  }

  updateMix() {
    if (this.nativeMode && this.store.transport.playing) {
      this.applyNativePan()
      this.queueNativeUpdate()
      return
    }
    this.applyMix()
  }

  applyNativePan() {
    for (const track of this.plan?.tracks || []) {
      const saved = this.store.project.tracks.find(t => t.id === track.id)
      if (saved) track.pan = saved.pan ?? 0
    }
    for (const source of this.sources.values()) {
      const track = this.store.project.tracks.find(t => t.id === source.trackId)
      if (track && source.panNode) source.panNode.port.postMessage({type:'pan',pan:track.pan ?? 0,monoInput:source.panNode.monoInput})
    }
  }
  nativeTopologySignature(tracks, mixer) {
    const sends = items => (items || []).map(send => send.to)
    return JSON.stringify({ ids: tracks.map(t => t.id),
      routes: tracks.map(t => [t.id, t.bus, sends(t.sends)]),
      buses: mixer.buses.map(b => [b.id, b.output, sends(b.sends)]),
      chains: [...tracks.map(t => t.fx), ...mixer.buses.map(b => b.effects), mixer.master.effects]
        .map(chain => chain.map(effect => [effect.type, effect.path || effect.plugin_path, effect.state_base64 || null])) })
  }

  queueNativeUpdate() {
    if (!this.nativeSessionId || !this.store.transport.playing) return
    if (this.nativeUpdating) { this.nativeUpdatePending = true; return }
    const generation = this.generation, sessionId = this.nativeSessionId
    const { tracks, mixer } = this.nativeConfiguration()
    const topology = this.nativeTopologySignature(tracks, mixer)
    if (topology !== this.nativeTopology) { this.fail(new Error('播放中的 VST 效果链、预设状态或路由已更改；请停止后重新播放。')); return }
    this.nativeUpdating = true
    fetch(apiUrl('/api/inference/vst/realtime/update'), { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({session_id:sessionId,tracks,mixer}) })
      .then(async response => {
        const data = await response.json().catch(() => ({}))
        if (!response.ok) throw new Error(data.detail || data.error || response.statusText)
        if (!this.isCurrent(generation) || sessionId !== this.nativeSessionId) return
        this.nativeTailMs = this.requestedNativeTail(tracks, mixer)
      })
      .catch(error=>{if(this.isCurrent(generation))this.fail(error)})
      .finally(()=>{if(sessionId===this.nativeSessionId) { this.nativeUpdating=false; if(this.nativeUpdatePending) { this.nativeUpdatePending=false; this.queueNativeUpdate() } } })
  }

  applyMix() {
    if (!this.context || !this.master) return
    const now = this.context.currentTime, tracks = this.plan?.tracks || []
    for (const t of tracks) {
      const saved = this.store.project.tracks.find(x => x.id === t.id)
      if (saved) Object.assign(t, { volume: saved.volume ?? 1, pan: saved.pan ?? 0, mute: !!saved.mute, solo: !!saved.solo,
        fx: saved.fx || [], sends: saved.sends || [], bus: saved.bus || 'master' })
    }
    const solo = tracks.some(t => t.solo)
    for (const t of tracks) {
      let n = this.trackNodes.get(t.id)
      if (!n) { n = this.createTrackNodes(t); this.trackNodes.set(t.id, n) }
      n.gain.gain.setTargetAtTime(this.nativeMode ? 1 : (!t.mute && (!solo || t.solo) ? t.volume * (t.bus_gain ?? 1) : 0), now, .015)
      for(const source of this.sources.values())if(source.trackId===t.id&&source.panNode)source.panNode.port.postMessage({type:'pan',pan:t.pan??0,monoInput:source.panNode.monoInput})
      const fxSignature = JSON.stringify(browserEffects(t.fx))
      if (n.fxSignature !== fxSignature) { n.fx.port.postMessage({type:'effects',effects:JSON.parse(fxSignature)}); n.fxSignature=fxSignature }
      const routeSignature = JSON.stringify([t.bus,t.sends || []])
      if (n.routeSignature !== routeSignature) { this.routeTrack(n,t); n.routeSignature=routeSignature }
    }
    const buses = this.store.project.mixer?.buses || []
    for (const b of buses) {
      const n=this.busNodes.get(b.id); if (!n) continue
      n.level.gain.setTargetAtTime(b.volume??1,now,.015)
      const fxSignature=JSON.stringify(browserEffects(b.effects))
      if(n.signature!==fxSignature){n.fx.port.postMessage({type:'effects',effects:JSON.parse(fxSignature)});n.signature=fxSignature}
      const pan=b.pan??0;if(n.panValue!==pan){n.fx.port.postMessage({type:'pan',pan});n.panValue=pan}
    }
    this.masterVolume.gain.setTargetAtTime(this.store.project.master_volume??1,now,.015)
    const masterFx=browserEffects(this.store.project.mixer?.master?.effects),signature=JSON.stringify(masterFx)
    if(this.masterFx&&this.masterFxSignature!==signature){this.masterFx.port.postMessage({type:'effects',effects:JSON.parse(signature)});this.masterFxSignature=signature}
  }

  createTrackNodes(t={}) {
    const gain=this.context.createGain()
    const fx=new AudioWorkletNode(this.context,WORKLET_PROCESSOR_NAME,{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[2],channelCount:2,channelCountMode:'explicit'})
    gain.connect(fx)
    const signature=JSON.stringify(browserEffects(t.fx));fx.port.postMessage({type:'effects',effects:JSON.parse(signature)})
    const node={gain,fx,fxSignature:signature,routeSignature:null,routeNodes:[]}
    this.routeTrack(node,t)
    node.routeSignature=JSON.stringify([t.bus||'master',t.sends||[]])
    return node
  }

  routeTrack(node, track) {
    node.fx.disconnect()
    for (const extra of node.routeNodes) { try { extra.disconnect() } catch {} }
    node.routeNodes=[]
    const busId=track.bus||'master', destination=this.busNodes.get(busId)?.input||this.master
    node.fx.connect(destination)
    for (const send of track.sends||[]) {
      const tap=this.context.createGain();tap.gain.value=send.level??1
      node.fx.connect(tap);tap.connect(this.busNodes.get(send.to)?.input||this.master);node.routeNodes.push(tap)
    }
  }

  ensureTrackNodes() {
    const tracks=this.plan?.tracks||[],buses=this.store.project.mixer?.buses||[],now=this.context.currentTime
    const desiredTracks=new Set(tracks.map(t=>t.id)),desired=new Set(buses.map(b=>b.id))
    for(const [id,n] of this.trackNodes)if(!desiredTracks.has(id)){n.gain.disconnect();n.fx.disconnect();for(const x of n.routeNodes||[])x.disconnect();this.trackNodes.delete(id)}
    for(const b of buses)if(!this.busNodes.has(b.id)){
      const input=this.context.createGain(),level=this.context.createGain(),fx=new AudioWorkletNode(this.context,WORKLET_PROCESSOR_NAME,{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[2],channelCount:2,channelCountMode:'explicit'}),output=this.context.createGain()
      input.connect(level);level.gain.value=b.volume??1;level.connect(fx);fx.connect(output)
      const signature=JSON.stringify(browserEffects(b.effects));fx.port.postMessage({type:'effects',effects:JSON.parse(signature)});fx.port.postMessage({type:'pan',pan:b.pan??0})
      this.busNodes.set(b.id,{input,level,fx,output,b,signature,panValue:b.pan??0,routeSignature:null,routeNodes:[]})
    }
    for(const [id,n] of this.busNodes)n.b=buses.find(b=>b.id===id)
    const pending=new Set(buses.map(b=>b.id))
    while(pending.size){
      const ready=[...pending].filter(id=>![...pending].some(source=>{const b=this.busNodes.get(source).b;return b.output===id||(b.sends||[]).some(s=>s.to===id)}))
      if(!ready.length)throw new Error('混音总线存在循环路由')
      for(const id of ready){
        const n=this.busNodes.get(id),b=n.b,sig=JSON.stringify([b.output,b.sends||[]])
        if(n.routeSignature!==sig){n.output.disconnect();for(const x of n.routeNodes)x.disconnect();n.routeNodes=[];n.output.connect(this.busNodes.get(b.output)?.input||this.master)
          for(const send of b.sends||[]){const tap=this.context.createGain();tap.gain.value=send.level??1;n.output.connect(tap);tap.connect(this.busNodes.get(send.to)?.input||this.master);n.routeNodes.push(tap)}n.routeSignature=sig}
        n.level.gain.setTargetAtTime(b.volume??1,now,.015)
        const fxSig=JSON.stringify(browserEffects(b.effects));if(n.signature!==fxSig){n.fx.port.postMessage({type:'effects',effects:JSON.parse(fxSig)});n.signature=fxSig}
        const pan=b.pan??0;if(n.panValue!==pan){n.fx.port.postMessage({type:'pan',pan});n.panValue=pan}
        pending.delete(id)
      }
    }
    for(const t of tracks){let n=this.trackNodes.get(t.id);if(!n){n=this.createTrackNodes(t);this.trackNodes.set(t.id,n)}const sig=JSON.stringify([t.bus||'master',t.sends||[]]);if(n.routeSignature!==sig){this.routeTrack(n,t);n.routeSignature=sig}}
    this.applyMix()
  }


  mixerRefreshed() {
    if (!this.context || !this.master) return
    if (this.nativeMode && this.store.transport.playing) {
      this.updateMix()
      return
    }
    this.applyMix()
    this.ensureTrackNodes()
    this.applyMix()
    if (this.store.transport.playing) this.update(this.generation)
  }

  invalidateProject() {
    this.projectChanged()
  }

  cancelSession(state, position, explicitTick) {
    ++this.generation
    this.closeNativeSession()
    this.cancelRequests()
    this.cancelFrame()
    this.stopSources()
    this.clockRunning = false
    this.heldMs = Math.max(0, position)
    this.store.transport.ms = this.heldMs
    this.store.transport.tick = explicitTick ?? this.msToTick(this.plan?.tempos || this.store.project.tempos, this.heldMs)
    this.store.transport.playing = false
    this.store.transport.state = state
    this.store.transport.pending = 0
    this.store.transport.error = ''
    if (state === 'paused') this.store.status = '已暂停'
    else if (state === 'idle') this.store.status = ''
  }

  stopSources() {
    for (const source of this.sources.values()) {
      source.onended = null
      try { source.stop() } catch { /* source may already have ended */ }
      try { source.disconnect(); source.panNode?.disconnect(); source.clipGain?.disconnect() } catch { /* node may already be disconnected */ }
    }
    this.sources.clear()
  }

  cancelRequests() {
    this.planController?.abort()
    this.planController = null
    for (const controller of this.requests.values()) controller.abort()
    this.requests.clear()
    this.store.transport.pending = 0
  }

  cancelFrame() {
    if (this.frameId) cancelAnimationFrame(this.frameId)
    this.frameId = 0
  }
  scheduleFrame(generation) {
    if (this.frameId || !this.isCurrent(generation)) return
    this.frameId = requestAnimationFrame(() => {
      this.frameId = 0
      this.update(generation)
    })
  }

  update(generation) {
    if (!this.isCurrent(generation) || !this.store.transport.playing || !this.plan) return
    const position = this.positionMs()
    const sourcePosition = this.nativeMode && this.clockRunning ? Math.max(this.heldMs, this.anchorMs + (this.context.currentTime - this.anchorTime) * 1000) : position
    this.store.transport.ms = Math.min(position, this.plan.end_ms)
    this.store.transport.tick = Math.min(this.plan.end_tick, this.msToTick(this.plan.tempos, this.store.transport.ms))
    this.prefetch(sourcePosition, generation)
    const missing = this.requiredMissing(sourcePosition)
    if (missing) { this.holdAt(sourcePosition, generation); this.scheduleFrame(generation); return }
    if (this.store.transport.state === 'buffering') {
      if (this.nativeMode) this.nativeHeardFrames = 0
      this.anchorMs = this.heldMs
      this.anchorTime = this.context.currentTime + 0.04
      if (this.nativeMode) this.nativeNode?.port.postMessage({type:'arm',startTime:this.anchorTime})
      this.clockRunning = true
      this.store.transport.state = 'playing'
      this.store.status = ''
    }
    this.scheduleSources(sourcePosition, generation)
    if (this.nativeMode) {
      this.nativeEndMs = this.timelineEndMs + this.nativeTailMs
      if (position >= this.nativeEndMs) { this.finishAtEnd(); return }
    } else if (position >= this.timelineEndMs) { this.finishAtEnd(); return }
    this.scheduleFrame(generation)
  }
  requiredMissing(position) {
    const tracks = new Map((this.plan?.tracks || []).map(track => [track.id, track]))
    const anySolo = [...tracks.values()].some(track => track.solo)
    for (const phrase of this.plan?.phrases || []) {
      if (phrase.clip_id) continue
      const track = tracks.get(phrase.track_id)
      if (!track || (!this.nativeMode && (track.mute || (anySolo && !track.solo)))) continue
      if (phrase.start_ms > position + 25 || phrase.end_ms + PHRASE_TAIL_MS < position) continue
      if (!this.buffers.has(this.phraseKey(phrase))) return phrase
    }
    for (const clip of this.store.project.audio_clips || []) {
      const startMs = this.tickToMs(this.plan.tempos, clip.start_tick || 0)
      const endMs = startMs + (clip.duration_seconds || 3600) * 1000
      if (startMs <= position + 25 && endMs >= position && !this.buffers.has(`clip:${clip.id}`)) return clip
    }
    return null
  }

  holdAt(boundaryMs, generation) {
    if (this.store.transport.state === 'buffering' && !this.clockRunning) return
    if (this.nativeMode && this.clockRunning) {
      this.startAt(this.msToTick(this.plan.tempos, this.positionMs()), Promise.resolve(), true)
      return
    }
    const position = this.positionMs()
    this.heldMs = Math.max(0, Math.min(position, boundaryMs))
    this.anchorMs = this.heldMs
    this.clockRunning = false
    this.stopSources()
    this.store.transport.ms = Math.min(this.heldMs, this.plan.end_ms)
    this.store.transport.tick = Math.min(this.plan.end_tick, this.msToTick(this.plan.tempos, this.store.transport.ms))
    this.store.transport.state = 'buffering'
    this.store.status = '试唱缓冲中…'
    this.prefetch(this.heldMs, generation)
    this.scheduleFrame(generation)
  }

  prefetch(position, generation) {
    const tracks = new Map((this.plan?.tracks || []).map(track => [track.id, track]))
    const anySolo = [...tracks.values()].some(track => track.solo)
    const candidates = this.plan.phrases.filter(phrase => {
      if (phrase.clip_id) return false
      const track = tracks.get(phrase.track_id)
      return track && (this.nativeMode || (!track.mute && (!anySolo || track.solo))) &&
        phrase.end_ms + PHRASE_TAIL_MS >= position && phrase.start_ms <= position + PREFETCH_WINDOW_MS
    })

    for (const [key, controller] of this.requests) {
      if (key.startsWith('clip:')) continue
      const phrase = this.plan.phrases.find(item => this.phraseKey(item) === key)
      if (!phrase || phrase.end_ms + PHRASE_TAIL_MS < position) {
        controller.abort()
        this.requests.delete(key)
      }
    }
    const ordered = [...candidates].sort((a, b) => {
      const aRequired = a.start_ms <= position && a.end_ms + PHRASE_TAIL_MS >= position ? 0 : 1
      const bRequired = b.start_ms <= position && b.end_ms + PHRASE_TAIL_MS >= position ? 0 : 1
      return aRequired - bRequired || a.start_ms - b.start_ms || a.track_id.localeCompare(b.track_id)
    })
    for (const phrase of ordered) {
      if (this.requests.size >= REQUEST_CONCURRENCY) break
      const key = this.phraseKey(phrase)
      if (this.buffers.has(key) || this.requests.has(key)) continue
      this.loadPhrase(phrase, generation)
    }
    this.store.transport.pending = this.requests.size
    for (const clip of this.store.project.audio_clips || []) {
      const startMs = this.tickToMs(this.plan.tempos, clip.start_tick || 0)
      if (startMs <= position + PREFETCH_WINDOW_MS && startMs + (clip.duration_seconds || 3600) * 1000 >= position) this.loadAudioClip(clip, generation)
    }
  }

  async loadAudioClip(clip, generation) {
    const key = `clip:${clip.id}`
    if (this.buffers.has(key) || this.requests.has(key)) return
    const controller = new AbortController(); this.requests.set(key, controller)
    try {
      const response = await fetch(apiUrl(`/api/media/clip/${encodeURIComponent(clip.id)}/pcm`), { signal: controller.signal })
      if (!this.isCurrent(generation) || controller.signal.aborted) return
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || response.statusText)
      const pcm = new Float32Array(await response.arrayBuffer())
      const channels = Number(response.headers.get('X-Channels')) || 2
      const rate = Number(response.headers.get('X-Sample-Rate')) || this.plan.sample_rate
      if (channels !== 2 || pcm.length % 2) throw new Error('音频剪辑 PCM 通道格式无效')
      const audio = this.context.createBuffer(2, pcm.length / 2, rate)
      audio.copyToChannel(pcm.filter((_, i) => i % 2 === 0), 0)
      audio.copyToChannel(pcm.filter((_, i) => i % 2 === 1), 1)
      const startTick = Number(response.headers.get('X-Start-Tick') || clip.start_tick || 0)
      const startMs = this.tickToMs(this.plan.tempos, startTick)
      this.buffers.set(key, { audio, used: performance.now(), bytes: pcm.byteLength, startMs,
        trackId: response.headers.get('X-Track-Id') || clip.track_id, gain: Number(response.headers.get('X-Gain') || 1),
        fadeInMs: Number(response.headers.get('X-Fade-In-Ms') || 0), fadeOutMs: Number(response.headers.get('X-Fade-Out-Ms') || 0), clip: true })
      this.bufferBytes += pcm.byteLength
      this.timelineEndMs = Math.max(this.timelineEndMs, startMs + audio.duration * 1000)
    } catch (error) { if (error?.name !== 'AbortError' && this.isCurrent(generation)) this.fail(error) }
    finally { if (this.requests.get(key) === controller) this.requests.delete(key); if (this.isCurrent(generation)) { this.store.transport.pending = this.requests.size; this.update(generation) } }
  }

  async loadPhrase(phrase, generation) {
    const key = this.phraseKey(phrase)
    const controller = new AbortController()
    this.requests.set(key, controller)
    this.store.transport.pending = this.requests.size
    try {
      const response = await fetch(apiUrl('/api/playback/phrase'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: this.plan.revision, track_id: phrase.track_id, phrase_index: phrase.phrase_index }),
        signal: controller.signal,
      })
      if (!this.isCurrent(generation) || controller.signal.aborted) return
      if (response.status === 409) {
        this.projectChanged()
        return
      }
      if (!response.ok) {
        const error = await response.json().catch(() => ({}))
        throw new Error(error.detail || error.error || response.statusText)
      }
      const raw = await response.arrayBuffer()
      if (!this.isCurrent(generation) || controller.signal.aborted) return
      const sampleRate = Number(response.headers.get('X-Sample-Rate')) || this.plan.sample_rate || 24000
      const channels = Number(response.headers.get('X-Channels')) || Number(phrase.channels) || Number(this.plan.channels) || 1
      const pcm = new Float32Array(raw)
      if (!pcm.length || pcm.length % channels) throw new Error('合成结果通道数据无效')
      const audio = this.context.createBuffer(channels, pcm.length / channels, sampleRate)
      for (let ch = 0; ch < channels; ch++) {
        const channel = new Float32Array(pcm.length / channels)
        for (let frame = 0; frame < channel.length; frame++) channel[frame] = pcm[frame * channels + ch]
        audio.copyToChannel(channel, ch)
      }
      this.buffers.set(key, { audio, used: performance.now(), bytes: pcm.byteLength })
      this.bufferBytes += pcm.byteLength
      this.timelineEndMs = Math.max(this.timelineEndMs, phrase.start_ms + audio.duration * 1000)
      this.pruneBuffers()
    } catch (error) {
      if (error?.name !== 'AbortError' && this.isCurrent(generation)) this.fail(error)
    } finally {
      if (this.requests.get(key) === controller) this.requests.delete(key)
      if (this.isCurrent(generation)) {
        this.store.transport.pending = this.requests.size
        if (this.store.transport.playing) this.update(generation)
      }
    }
  }


  pruneBuffers() {
    if (this.bufferBytes <= MAX_CACHE_BYTES) return
    const protectedKeys = new Set(this.sources.keys())
    const nowMs = this.positionMs()
    for (const phrase of this.plan?.phrases || []) {
      if (phrase.start_ms <= nowMs && phrase.end_ms + PHRASE_TAIL_MS >= nowMs) protectedKeys.add(this.phraseKey(phrase))
    }
    const entries = [...this.buffers.entries()].sort((a, b) => a[1].used - b[1].used)
    for (const [key, entry] of entries) {
      if (this.bufferBytes <= MAX_CACHE_BYTES || protectedKeys.has(key)) continue
      this.buffers.delete(key)
      this.bufferBytes -= entry.bytes
    }
  }

  scheduleSources(position, generation) {
    const scheduleUntil = position + SCHEDULE_AHEAD_MS
    const tracks = new Map((this.plan?.tracks || []).map(track => [track.id, track]))
    const anySolo = [...tracks.values()].some(track => track.solo)
    for (const phrase of this.plan.phrases) {
      if (phrase.start_ms > scheduleUntil) break
      if (phrase.clip_id) continue
      const track = tracks.get(phrase.track_id)
      if (!track || (!this.nativeMode && (track.mute || (anySolo && !track.solo)))) continue
      const key = this.phraseKey(phrase)
      if (this.sources.has(key) || this.completed.has(key)) continue
      const cached = this.buffers.get(key)
      if (!cached) continue
      cached.used = performance.now()
      const offset = Math.max(0, (position - phrase.start_ms) / 1000)
      if (offset >= cached.audio.duration) continue
      const source=this.context.createBufferSource()
      source.buffer=cached.audio
      const panNode=new AudioWorkletNode(this.context,WORKLET_PROCESSOR_NAME,{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[2],channelCount:2,channelCountMode:'explicit'})
      panNode.monoInput=cached.audio.numberOfChannels===1;panNode.port.postMessage({type:'pan',pan:track.pan??0,monoInput:panNode.monoInput})
      source.trackId=phrase.track_id;source.panNode=panNode
      source.connect(panNode)
      panNode.connect(this.trackNodes.get(phrase.track_id).gain)
      const plannedTime = this.anchorTime + (phrase.start_ms - this.anchorMs) / 1000
      const startAt = Math.max(plannedTime, this.context.currentTime + 0.005)
      source.onended = () => {
        if (this.sources.get(key) === source) { this.sources.delete(key); this.completed.add(key) }
        try { source.disconnect(); source.panNode?.disconnect() } catch { /* 已断开 */ }
      }
      this.sources.set(key, source)
      try { source.start(startAt, offset) } catch (error) {
        this.sources.delete(key); source.disconnect(); source.panNode?.disconnect(); this.fail(error); return
      }
    }
    for (const clip of this.store.project.audio_clips || []) {
      const key=`clip:${clip.id}`,cached=this.buffers.get(key)
      if(!cached||this.sources.has(key)||this.completed.has(key))continue
      const track=tracks.get(cached.trackId)||this.store.project.tracks.find(t=>t.id===cached.trackId);if(!track||(!this.nativeMode&&(track.mute||(anySolo&&!track.solo))))continue
      const end=cached.startMs+cached.audio.duration*1000;if(cached.startMs>scheduleUntil||end<=position)continue
      const offset=Math.max(0,(position-cached.startMs)/1000),source=this.context.createBufferSource(),gain=this.context.createGain();source.buffer=cached.audio
      const when=Math.max(this.anchorTime+(cached.startMs-this.anchorMs)/1000,this.context.currentTime+.005),duration=cached.audio.duration,now=this.context.currentTime
      gain.gain.value=cached.gain
      if(cached.fadeInMs>0&&offset<cached.fadeInMs/1000){gain.gain.setValueAtTime(0,when);gain.gain.linearRampToValueAtTime(cached.gain,when+cached.fadeInMs/1000-offset)}
      if(cached.fadeOutMs>0){const fadeSeconds=cached.fadeOutMs/1000,fadeStart=Math.max(0,duration-offset-fadeSeconds);if(offset>=duration-fadeSeconds)gain.gain.setValueAtTime(cached.gain*(duration-offset)/fadeSeconds,when);else gain.gain.setValueAtTime(cached.gain,when+fadeStart);gain.gain.linearRampToValueAtTime(0,when+duration-offset)}
      const panNode=new AudioWorkletNode(this.context,WORKLET_PROCESSOR_NAME,{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[2],channelCount:2,channelCountMode:'explicit'});panNode.monoInput=cached.audio.numberOfChannels===1;panNode.port.postMessage({type:'pan',pan:track.pan??0,monoInput:panNode.monoInput});source.trackId=cached.trackId;source.panNode=panNode;source.clipGain=gain;source.connect(panNode);panNode.connect(gain);if(this.nativeMode)gain.connect(this.nativeNode,0,this.nativeTrackIds.indexOf(cached.trackId));else gain.connect(this.trackNodes.get(cached.trackId).gain);this.sources.set(key,source)
      source.onended=()=>{this.sources.delete(key);this.completed.add(key);try{source.disconnect();panNode.disconnect();gain.disconnect()}catch{}}
      try{source.start(when,offset)}catch(error){this.sources.delete(key);source.disconnect();panNode.disconnect();gain.disconnect();this.fail(error);return}
    }
  }

  phraseKey(phrase) {
    return `${phrase.track_id}\u0000${phrase.phrase_index}`
  }

  finishAtEnd() {
    const endTick = this.plan?.end_tick ?? this.store.transport.tick
    const endMs = this.plan?.end_ms ?? this.store.transport.ms
    this.cancelSession('idle', endMs, endTick)
    this.store.transport.state = 'idle'
    this.store.status = ''
  }

  fail(error) {
    const message = error?.message || String(error)
    this.cancelSession('error', this.positionMs())
    this.store.transport.state = 'error'
    this.store.transport.error = message
    this.store.status = `试唱失败：${message}`
  }

  dispose() {
    this.destroyed = true
    this.cancelSession('idle', this.positionMs())
    this.plan = null
    this.buffers.clear()
    this.bufferBytes = 0
    for (const nodes of this.trackNodes.values()) {
      nodes.gain.disconnect(); nodes.fx.disconnect()
      for (const extra of nodes.routeNodes || []) extra.disconnect()
    }
    for (const n of this.busNodes.values()) { n.input.disconnect(); n.level.disconnect(); n.fx.disconnect(); n.output.disconnect(); for (const extra of n.routeNodes || []) extra.disconnect() }
    this.trackNodes.clear(); this.busNodes.clear()
    if (this.context && this.context.state !== 'closed') this.context.close().catch(() => {})
  }
}
