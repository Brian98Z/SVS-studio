export const NATIVE_MIXER_PROCESSOR_NAME = 'svs-native-mixer'

// Transfer each contiguous track-major PCM block directly to the stream worker;
// output buffers return over the same channel without renderer-thread copies.
export const nativeMixerProcessorSource = `class SvsNativeMixer extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const o = options.processorOptions || {}
    this.tracks = o.tracks || []
    this.blockFrames = o.blockFrames || 1024
    this.targetBlocks = o.targetBlocks || 4
    this.captureOnly = !!o.captureOnly
    this.capacity = this.targetBlocks + 3
    this.read = 0
    this.captureSamples = this.tracks.length * this.blockFrames * 2
    // Five queued requests, one in flight, one block being captured.
    this.capturePool = Array.from({ length: 7 }, () => new Float32Array(this.captureSamples))
    this.capture = this.capturePool.pop()
    this.frames = 0
    this.queue = []
    this.started = false
    this.armed = false
    this.startTime = Infinity
    this.playedFrames = 0
    this.streamPort = null
    this.port.onmessage = e => {
      const m = e.data || {}
      if (m.type === 'stream') { this.streamPort = m.port; this.streamPort.onmessage = event => this.receiveOutput(event.data); this.streamPort.start(); return }
      if (m.type === 'output') {
        this.receiveOutput(m)
      } else if (m.type === 'flush') {
        this.armed = false
        this.queue.length = 0; this.read = 0; this.started = false
        this.capture?.fill(0); this.frames = 0
      } else if (m.type === 'arm') {
        this.startTime = m.startTime
        this.armed = true
      }
    }
  }
  send(message, transfer = []) { (this.streamPort || this.port).postMessage(message, transfer) }
  receiveOutput(m) {
    if (m.type === 'recycle') {
      if (m.samples instanceof ArrayBuffer && m.samples.byteLength === this.captureSamples * 4 && this.capturePool.length < 7) {
        this.capturePool.push(new Float32Array(m.samples))
      }
      return
    }
    if (!(m.samples instanceof ArrayBuffer)) return
    if (this.queue.length >= this.capacity) this.send({ type: 'overflow' })
    else {
      this.queue.push(new Float32Array(m.samples))
      if (!this.started && this.queue.length >= this.targetBlocks) this.started = true
    }
  }
  process(inputs, outputs) {
    const out = outputs[0]
    if (!this.armed || currentTime < this.startTime) return true
    const frames = out?.[0]?.length || 128
    for (let c = 0; c < (out?.length || 0); c++) out[c].fill(0)
    if (!this.capture) {
      this.capture = this.capturePool.pop()
      if (!this.capture) {
        this.armed = false
        this.send({ type: 'overflow' })
        return true
      }
      this.capture.fill(0)
    }
    for (let t = 0; t < this.tracks.length; t++) {
      const input = inputs[t]
      const left = input?.[0], right = input?.[1] || left
      const base = t * this.blockFrames * 2
      if (!left) continue
      for (let f = 0; f < frames; f++) {
        const at = (this.frames + f) * 2
        this.capture[base + at] = left[f] || 0
        this.capture[base + at + 1] = right[f] || 0
      }
    }
    this.frames += frames
    if (this.frames >= this.blockFrames) {
      const samples = this.capture.buffer
      this.send({ type: 'capture', samples }, [samples])
      this.capture = null
      this.frames = 0
    }
    if (this.captureOnly) return true
    if (this.started && this.queue.length) {
      const current = this.queue[0]
      const count = Math.min(frames, current.length / 2 - this.read)
      for (let f = 0; f < count; f++) {
        const at = (this.read + f) * 2
        if (out?.[0]) out[0][f] = current[at]
        if (out?.[1]) out[1][f] = current[at + 1]
      }
      this.read += count
      if (this.read >= current.length / 2) { this.queue.shift(); this.read = 0 }
      this.playedFrames += count
      if (this.playedFrames % this.blockFrames === 0) this.send({ type: 'clock', frames: this.playedFrames })
    } else if (this.started) {
      this.send({ type: 'underrun' })
      this.started = false
    }
    return true
  }
}
registerProcessor('svs-native-mixer', SvsNativeMixer);`
