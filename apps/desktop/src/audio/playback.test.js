import test from 'node:test'
import assert from 'node:assert/strict'
import { PlaybackEngine } from './playback.js'

const vst = seconds => ({ type: 'vst3', enabled: true, tail_seconds: seconds })
const tail = (tracks, mixer) => PlaybackEngine.prototype.requestedNativeTail(tracks, mixer)

test('serial tails add while parallel bus paths use the longest arrival', () => {
  const tracks = [{ fx: [vst(.3), vst(.4)], bus: 'dry', sends: [{ to: 'wet', level: 1 }] }]
  const mixer = { master: { effects: [vst(.2)] }, buses: [
    { id: 'dry', effects: [vst(.5)], output: 'master' },
    { id: 'wet', effects: [vst(.8)], output: 'master' },
  ] }
  assert.ok(Math.abs(tail(tracks, mixer) - 1700) < 1e-6)
})

test('nested bus sends include downstream effects exactly once per path', () => {
  const tracks = [{ fx: [vst(1)], bus: 'a' }, { fx: [vst(.1)], bus: 'b' }]
  const mixer = { master: { effects: [vst(.5)] }, buses: [
    { id: 'a', effects: [vst(2)], output: 'master', sends: [{ to: 'b' }] },
    { id: 'b', effects: [vst(3)], output: 'master' },
  ] }
  assert.equal(tail(tracks, mixer), 6500)
})

test('bypassed effects and disconnected buses do not extend the tail', () => {
  const tracks = [{ fx: [{ ...vst(10), enabled: false }, vst(.25)] }]
  const mixer = { master: {}, buses: [{ id: 'unused', effects: [vst(10)], output: 'master' }] }
  assert.equal(tail(tracks, mixer), 250)
  assert.equal(tail([], mixer), 0)
})

test('invalid or cyclic reachable routes fail rather than hanging playback', () => {
  assert.throws(() => tail([{ bus: 'missing' }], { buses: [] }), /路由/)
  assert.throws(() => tail([{ bus: 'a' }], { buses: [
    { id: 'a', output: 'b' }, { id: 'b', output: 'a' },
  ] }), /循环/)
})

test('native playback waits for initially inaudible phrases before starting', () => {
  for (const tracks of [
    [{ id: 'piano', mute: true }],
    [{ id: 'piano' }, { id: 'other', solo: true }],
  ]) {
    const engine = new PlaybackEngine({ project: { audio_clips: [] } })
    const phrase = { track_id: 'piano', phrase_index: 0, start_ms: 1000, end_ms: 2000 }
    engine.plan = { tracks, phrases: [phrase] }
    engine.nativeMode = false
    assert.equal(engine.requiredMissing(1000), null)
    engine.nativeMode = true
    assert.equal(engine.requiredMissing(974), null)
    assert.equal(engine.requiredMissing(975), phrase)
    assert.equal(engine.requiredMissing(2251), null)
    engine.buffers.set(engine.phraseKey(phrase), { audio: {} })
    assert.equal(engine.requiredMissing(1000), null)
    engine.buffers.clear()
    assert.equal(engine.requiredMissing(1000), phrase)
  }
})

test('native audible clock holds during plugin startup then includes the drained end', () => {
  const engine = new PlaybackEngine({ project: {} })
  engine.context = { sampleRate: 48000 }
  engine.clockRunning = true
  engine.nativeMode = true
  engine.nativeLatencySamples = 480
  engine.anchorMs = 1200
  engine.nativeHeardFrames = 240
  assert.equal(engine.positionMs(), 1200)
  engine.nativeHeardFrames = 480
  assert.equal(engine.positionMs(), 1200)
  engine.nativeHeardFrames = 24480
  assert.equal(engine.positionMs(), 1700)
})

test('native audition rejects unsupported block sizes and prebuffer counts before opening streams', async () => {
  for (const audition of [
    { block_frames: 768, target_blocks: 2 },
    { block_frames: 512, target_blocks: 0 },
    { block_frames: 1024, target_blocks: 3 },
    { block_frames: '1024', target_blocks: 2 },
  ]) {
    const engine = new PlaybackEngine({ audition, project: {} })
    await assert.rejects(engine.openNativeSession(engine.generation), /试听分块或预缓冲设置无效/)
  }
})
