// Keep sequential native HTTP processing independent of renderer work.
let audioPort, sessionId, endpoint, frames, rate, targetBlocks
let closed = false, busy = false, controller = null, processed = 0
let underruns = 0, lastReport = 0
let deviceOutput = null, deviceTimer = null, devicePolling = false

async function pollDevice() {
  if (closed || !deviceOutput || devicePolling) return
  devicePolling = true
  try {
    const response = await fetch(deviceOutput.endpoint)
    const status = await response.json()
    if (closed) return
    if (!response.ok || status.error) throw new Error(status.detail || status.error || '原生声卡状态读取失败')
    self.postMessage({ type: 'clock', frames: status.played_frames })
    self.postMessage({ type: 'stats', stats: { device_queue_ms: status.queued_frames * 1000 / rate,
      device_underruns: status.underruns, device: status.device, output: 'device' } })
  } catch (error) { if (!closed) fail(error.message) }
  finally { devicePolling = false }
}

async function sendDevice(samples, sequence) {
  for (let retries = 0; !closed && retries < 20; retries++) {
    const response = await fetch(`${deviceOutput.endpoint}/pcm`, { method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', 'x-audio-sequence': String(sequence) }, body: samples, signal: controller.signal })
    const status = await response.json()
    if (response.ok) return
    if (response.status !== 409 || status.error !== 'OUTPUT_FULL') throw new Error(status.detail || status.error || '原生声卡提交失败')
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  if (!closed) throw new Error('原生声卡队列持续过载；播放已停止')
}
const queue = []

function report(force = false) {
  if (!force && performance.now() - lastReport < 100) return
  lastReport = performance.now()
  self.postMessage({ type: 'stats', stats: { processed_blocks: processed,
    processing_ms: processingMs, underruns, latency_ms: targetBlocks * frames * 1000 / rate } })
}
let processingMs = 0
function fail(message) {
  if (closed) return
  closed = true
  clearInterval(deviceTimer)
  queue.length = 0
  controller?.abort()
  audioPort?.close()
  self.postMessage({ type: 'error', message })
}
async function pump() {
  if (closed || busy || !queue.length) return
  busy = true
  const samples = queue.shift()
  controller = new AbortController()
  try {
    const response = await fetch(endpoint, { method: 'POST', headers: {
      'Content-Type': 'application/octet-stream', 'X-Audio-Session-Id': sessionId,
      'X-Audio-Sequence': String(processed)
    }, body: samples, signal: controller.signal })
    if (closed) return
    if (!response.ok) {
      let detail = response.statusText
      try { const data = await response.json(); detail = data.detail || data.error || detail } catch {}
      throw new Error(detail)
    }
    const sequence = Number(response.headers.get('X-Audio-Sequence'))
    const responseFrames = Number(response.headers.get('X-Audio-Frames'))
    if (sequence !== processed || responseFrames !== frames) throw new Error('原生混音器返回块序号或长度无效')
    const output = await response.arrayBuffer()
    if (output.byteLength !== frames * 8) throw new Error('原生混音器返回PCM长度无效')
    if (deviceOutput) await sendDevice(output, processed)
    else audioPort.postMessage({ type: 'output', samples: output }, [output])
    processed++
    processingMs = Number(response.headers.get('X-Audio-Processing-Ms')) || 0
    report()
  } catch (error) {
    if (!closed) fail(error.message || String(error))
  } finally {
    if (!closed) audioPort.postMessage({ type: 'recycle', samples }, [samples])
    busy = false
    controller = null
    if (!closed) pump()
  }
}
let trackIds = []
self.onmessage = ({ data }) => {
  if (data.type !== 'init' || audioPort || closed) return
  ;({ sessionId, endpoint, frames, rate, targetBlocks, trackIds } = data)
  deviceOutput = data.deviceOutput || null
  if (deviceOutput) deviceTimer = setInterval(pollDevice, 25)
  audioPort = data.port
  audioPort.onmessage = ({ data: message }) => {
    if (closed) return
    if (message.type === 'capture') {
      if (queue.length >= 5) { fail('原生混音器输入缓冲溢出；播放已停止以避免丢帧。'); return }
      if (!(message.samples instanceof ArrayBuffer) || message.samples.byteLength !== frames * trackIds.length * 8) { fail('原生混音器输入PCM长度无效'); return }
      queue.push(message.samples)
      pump()
    } else if (message.type === 'clock') {
      if (!deviceOutput) self.postMessage(message)
    } else if (message.type === 'underrun') {
      underruns++
      report(true)
    } else if (message.type === 'overflow') fail('原生混音器缓冲溢出；播放已停止以避免丢帧。')
  }
  audioPort.start()
  self.postMessage({ type: 'ready' })
}
