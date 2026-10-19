import { WORKLET_PROCESSOR_NAME, workletProcessorSource } from './fx.js'
import { apiUrl } from './origin.js'
const REQUEST_CONCURRENCY = 2
const PREFETCH_WINDOW_MS = 6000
const SCHEDULE_AHEAD_MS = 350
const MAX_CACHE_BYTES = 64 * 1024 * 1024
const PHRASE_TAIL_MS = 250

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
  }

  contextForGesture() {
    if (!this.context) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext
      if (!AudioContextClass) throw new Error('此浏览器不支持 Web Audio')
      this.context = new AudioContextClass()
      this.workletUrl = URL.createObjectURL(new Blob([workletProcessorSource], { type: 'text/javascript' }))
      this.workletReady = this.context.audioWorklet.addModule(this.workletUrl)
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
      // resume() is invoked before any await, while the originating gesture is active.
      resume = context.resume()
    } catch (error) {
      this.fail(error)
      return Promise.resolve()
    }
    return this.startAt(tick, resume)
  }

  seek(tick) {
    return this.play(tick)
  }

  async startAt(tick, resumePromise) {
    const generation = ++this.generation
    this.cancelRequests()
    this.stopSources()
    this.completed.clear()
    this.cancelFrame()
    this.heldMs = Math.max(0, this.tickToMs(this.store.project.tempos, tick))
    this.anchorMs = this.heldMs
    this.anchorTime = this.context.currentTime
    this.clockRunning = false
    this.store.transport.tick = Math.max(0, tick)
    this.store.transport.ms = this.heldMs
    this.store.transport.playing = true
    this.store.transport.state = 'buffering'
    this.store.transport.pending = this.plan ? 0 : 1
    this.store.transport.error = ''
    this.store.status = this.plan ? '试唱缓冲中…' : '正在读取试唱计划…'

    try {
      this.masterFx = this.masterFx || new AudioWorkletNode(this.context, WORKLET_PROCESSOR_NAME, { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2], channelCount: 2, channelCountMode: 'explicit' })
      if (!this.masterFxConnected) { this.masterVolume.connect(this.masterFx); this.masterFx.connect(this.context.destination); this.masterFxConnected = true }
      const fx = this.store.project.mixer?.master?.effects || []
      if (this.masterFxSignature !== JSON.stringify(fx)) { this.masterFx.port.postMessage({ type: 'effects', effects: JSON.parse(JSON.stringify(fx)) }); this.masterFxSignature = JSON.stringify(fx) }
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
      const startMs = Math.max(0, Math.min(this.tickToMs(plan.tempos, tick), plan.end_ms))
      this.store.transport.tick = Math.max(0, Math.min(tick, plan.end_tick))
      this.store.transport.ms = startMs
      this.heldMs = startMs
      this.anchorMs = startMs
      this.anchorTime = this.context.currentTime + 0.06
      this.clockRunning = true
      this.ensureTrackNodes()
      if (!plan.phrases.length || startMs >= plan.end_ms) {
        this.finishAtEnd()
        return
      }
      this.store.transport.state = 'playing'
      this.store.status = ''
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
    this.applyMix()
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
      n.gain.gain.setTargetAtTime(!t.mute && (!solo || t.solo) ? t.volume * (t.bus_gain ?? 1) : 0, now, .015)
      for(const source of this.sources.values())if(source.trackId===t.id&&source.panNode)source.panNode.port.postMessage({type:'pan',pan:t.pan??0,monoInput:source.panNode.monoInput})
      const fxSignature = JSON.stringify(t.fx || [])
      if (n.fxSignature !== fxSignature) { n.fx.port.postMessage({type:'effects',effects:JSON.parse(fxSignature)}); n.fxSignature=fxSignature }
      const routeSignature = JSON.stringify([t.bus,t.sends || []])
      if (n.routeSignature !== routeSignature) { this.routeTrack(n,t); n.routeSignature=routeSignature }
    }
    const buses = this.store.project.mixer?.buses || []
    for (const b of buses) {
      const n=this.busNodes.get(b.id); if (!n) continue
      n.level.gain.setTargetAtTime(b.volume??1,now,.015)
      const fxSignature=JSON.stringify(b.effects||[])
      if(n.signature!==fxSignature){n.fx.port.postMessage({type:'effects',effects:JSON.parse(fxSignature)});n.signature=fxSignature}
      const pan=b.pan??0;if(n.panValue!==pan){n.fx.port.postMessage({type:'pan',pan});n.panValue=pan}
    }
    this.masterVolume.gain.setTargetAtTime(this.store.project.master_volume??1,now,.015)
    const masterFx=this.store.project.mixer?.master?.effects||[],signature=JSON.stringify(masterFx)
    if(this.masterFx&&this.masterFxSignature!==signature){this.masterFx.port.postMessage({type:'effects',effects:JSON.parse(signature)});this.masterFxSignature=signature}
  }

  createTrackNodes(t={}) {
    const gain=this.context.createGain()
    const fx=new AudioWorkletNode(this.context,WORKLET_PROCESSOR_NAME,{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[2],channelCount:2,channelCountMode:'explicit'})
    gain.connect(fx)
    const signature=JSON.stringify(t.fx||[]);fx.port.postMessage({type:'effects',effects:JSON.parse(signature)})
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
      const signature=JSON.stringify(b.effects||[]);fx.port.postMessage({type:'effects',effects:JSON.parse(signature)});fx.port.postMessage({type:'pan',pan:b.pan??0})
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
        const fxSig=JSON.stringify(b.effects||[]);if(n.signature!==fxSig){n.fx.port.postMessage({type:'effects',effects:JSON.parse(fxSig)});n.signature=fxSig}
        const pan=b.pan??0;if(n.panValue!==pan){n.fx.port.postMessage({type:'pan',pan});n.panValue=pan}
        pending.delete(id)
      }
    }
    for(const t of tracks){let n=this.trackNodes.get(t.id);if(!n){n=this.createTrackNodes(t);this.trackNodes.set(t.id,n)}const sig=JSON.stringify([t.bus||'master',t.sends||[]]);if(n.routeSignature!==sig){this.routeTrack(n,t);n.routeSignature=sig}}
    this.applyMix()
  }


  mixerRefreshed() {
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
    this.store.transport.ms = Math.min(position, this.plan.end_ms)
    this.store.transport.tick = Math.min(this.plan.end_tick, this.msToTick(this.plan.tempos, this.store.transport.ms))
    this.prefetch(position, generation)
    const missing = this.requiredMissing(position)
    if (missing) {
      this.holdAt(position, generation)
      return
    }
    if (this.store.transport.state === 'buffering') {
      this.anchorMs = this.heldMs
      this.anchorTime = this.context.currentTime + 0.04
      this.clockRunning = true
      this.store.transport.state = 'playing'
      this.store.status = ''
    }
    this.scheduleSources(position, generation)
    if (position >= this.timelineEndMs) {
      this.finishAtEnd()
      return
    }
    this.scheduleFrame(generation)
  }

  requiredMissing(position) {
    const tracks = new Map((this.plan?.tracks || []).map(track => [track.id, track]))
    const anySolo = [...tracks.values()].some(track => track.solo)
    for (const phrase of this.plan?.phrases || []) {
      if (phrase.clip_id) continue
      const track = tracks.get(phrase.track_id)
      if (!track || track.mute || (anySolo && !track.solo)) continue
      if (phrase.start_ms > position + 25 || phrase.end_ms + PHRASE_TAIL_MS < position) continue
      if (!this.buffers.has(this.phraseKey(phrase))) return phrase
    }
    return null
  }

  holdAt(boundaryMs, generation) {
    if (this.store.transport.state === 'buffering' && !this.clockRunning) return
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
      return track && !track.mute && (!anySolo || track.solo) &&
        phrase.end_ms + PHRASE_TAIL_MS >= position && phrase.start_ms <= position + PREFETCH_WINDOW_MS
    })

    for (const [key, controller] of this.requests) {
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
      if (!track || track.mute || (anySolo && !track.solo)) continue
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
      source.connect(panNode);panNode.connect(this.trackNodes.get(phrase.track_id).gain)
      const plannedTime = this.anchorTime + (phrase.start_ms - this.anchorMs) / 1000
      const startAt = Math.max(plannedTime, this.context.currentTime + 0.005)
      source.onended = () => {
        if (this.sources.get(key) === source) {
          this.sources.delete(key)
          this.completed.add(key)
        }
        try { source.disconnect(); source.panNode?.disconnect() } catch { /* 已断开 */ }
      }
      this.sources.set(key, source)
      try { source.start(startAt, offset) } catch (error) {
        this.sources.delete(key)
        source.disconnect(); source.panNode?.disconnect()
        this.fail(error)
        return
      }
    }
    for (const clip of this.store.project.audio_clips || []) {
      const key=`clip:${clip.id}`,cached=this.buffers.get(key)
      if(!cached||this.sources.has(key)||this.completed.has(key))continue
      const track=tracks.get(cached.trackId)||this.store.project.tracks.find(t=>t.id===cached.trackId);if(!track||track.mute||(anySolo&&!track.solo))continue
      const end=cached.startMs+cached.audio.duration*1000;if(cached.startMs>scheduleUntil||end<=position)continue
      const offset=Math.max(0,(position-cached.startMs)/1000),source=this.context.createBufferSource(),gain=this.context.createGain();source.buffer=cached.audio
      const when=Math.max(this.anchorTime+(cached.startMs-this.anchorMs)/1000,this.context.currentTime+.005),duration=cached.audio.duration,now=this.context.currentTime
      gain.gain.value=cached.gain
      if(cached.fadeInMs>0&&offset<cached.fadeInMs/1000){gain.gain.setValueAtTime(0,when);gain.gain.linearRampToValueAtTime(cached.gain,when+cached.fadeInMs/1000-offset)}
      if(cached.fadeOutMs>0){const fadeSeconds=cached.fadeOutMs/1000,fadeStart=Math.max(0,duration-offset-fadeSeconds);if(offset>=duration-fadeSeconds)gain.gain.setValueAtTime(cached.gain*(duration-offset)/fadeSeconds,when);else gain.gain.setValueAtTime(cached.gain,when+fadeStart);gain.gain.linearRampToValueAtTime(0,when+duration-offset)}
      const panNode=new AudioWorkletNode(this.context,WORKLET_PROCESSOR_NAME,{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[2],channelCount:2,channelCountMode:'explicit'});panNode.monoInput=cached.audio.numberOfChannels===1;panNode.port.postMessage({type:'pan',pan:track.pan??0,monoInput:panNode.monoInput});source.trackId=cached.trackId;source.panNode=panNode;source.clipGain=gain;source.connect(panNode);panNode.connect(gain);gain.connect(this.trackNodes.get(cached.trackId).gain);this.sources.set(key,source)
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
