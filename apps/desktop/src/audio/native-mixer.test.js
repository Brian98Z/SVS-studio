import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { nativeMixerProcessorSource } from './native-mixer.js'

function worklet() {
  let Processor
  const messages = []
  const context = vm.createContext({ Float32Array, ArrayBuffer, currentTime: 0,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: m => messages.push(m) } } },
    registerProcessor: (_, type) => { Processor = type } })
  vm.runInContext(nativeMixerProcessorSource, context)
  const node = new Processor({ processorOptions: { tracks: ['a'], blockFrames: 256, targetBlocks: 2 } })
  node.port.onmessage({ data: { type: 'arm', startTime: 0 } })
  return { node, messages, render: () => {
    const out = [new Float32Array(128), new Float32Array(128)]
    node.process([], [out])
    return out[0]
  }, push: value => node.receiveOutput({ samples: new Float32Array(512).fill(value).buffer }) }
}

test('underrun inserts silence then resumes queued PCM in order after refill', () => {
  const { render, push, messages } = worklet()
  push(.25); push(.5)
  assert.equal(render()[0], .25)
  assert.equal(render()[0], .25)
  assert.equal(render()[0], .5)
  assert.equal(render()[0], .5)
  assert.equal(render()[0], 0)
  assert.equal(messages.filter(m => m.type === 'underrun').length, 1)
  push(.75)
  assert.equal(render()[0], 0)
  push(1)
  assert.equal(render()[0], .75)
  assert.equal(render()[0], .75)
  assert.equal(render()[0], 1)
})

test('bounded output queue signals overflow without discarding earlier PCM', () => {
  const { render, push, messages } = worklet()
  for (let i = 1; i <= 6; i++) push(i)
  assert.equal(messages.filter(m => m.type === 'overflow').length, 1)
  for (let i = 1; i <= 5; i++) {
    assert.equal(render()[0], i)
    assert.equal(render()[0], i)
  }
})

test('device capture never also plays queued audio through the browser', () => {
  let Processor
  const messages = []
  const context = vm.createContext({ Float32Array, ArrayBuffer, currentTime: 0,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: m => messages.push(m) } } },
    registerProcessor: (_, type) => { Processor = type } })
  vm.runInContext(nativeMixerProcessorSource, context)
  const node = new Processor({ processorOptions: { tracks: ['a'], blockFrames: 128, targetBlocks: 1, captureOnly: true } })
  node.port.onmessage({ data: { type: 'arm', startTime: 0 } })
  node.receiveOutput({ samples: new Float32Array(256).fill(.75).buffer })
  const out = [new Float32Array(128).fill(1), new Float32Array(128).fill(1)]
  node.process([[new Float32Array(128).fill(.25), new Float32Array(128).fill(-.5)]], [out])
  assert.ok(out.every(channel => channel.every(sample => sample === 0)))
  const capture = new Float32Array(messages.find(message => message.type === 'capture').samples)
  assert.equal(capture[0], .25)
  assert.equal(capture[1], -.5)
  assert.equal(messages.some(message => message.type === 'clock'), false)
})

test('capture preserves track and stereo order across blocks without reusing prior PCM', () => {
  let Processor
  const messages = []
  const context = vm.createContext({ Float32Array, ArrayBuffer, currentTime: 0,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: m => messages.push(m) } } },
    registerProcessor: (_, type) => { Processor = type } })
  vm.runInContext(nativeMixerProcessorSource, context)
  const node = new Processor({ processorOptions: { tracks: ['a', 'b'], blockFrames: 256, captureOnly: true } })
  node.port.onmessage({ data: { type: 'arm', startTime: 0 } })
  const out = [new Float32Array(128), new Float32Array(128)]
  const channel = value => new Float32Array(128).fill(value)
  node.process([[channel(.25), channel(-.5)], [channel(.75)]], [out])
  node.process([[channel(1), channel(-1)]], [out])
  node.process([], [out]); node.process([], [out])
  const captures = messages.filter(m => m.type === 'capture').map(m => new Float32Array(m.samples))
  assert.deepEqual(Array.from(captures[0].slice(0, 4)), [.25, -.5, .25, -.5])
  assert.deepEqual(Array.from(captures[0].slice(256, 260)), [1, -1, 1, -1])
  assert.ok(captures[0].slice(512, 768).every(value => value === .75))
  assert.ok(captures[0].slice(768).every(value => value === 0))
  assert.ok(captures[1].every(value => value === 0))
})

test('capture stops explicitly on a stalled worker rather than dropping or allocating more PCM', () => {
  const { node, messages, render } = worklet()
  for (let i = 0; i < 16; i++) render()
  const captured = messages.filter(m => m.type === 'capture')
  assert.equal(captured.length, 7)
  assert.equal(messages.filter(m => m.type === 'overflow').length, 1)
  assert.equal(node.armed, false)
  render()
  assert.equal(messages.filter(m => m.type === 'capture').length, 7)
})

test('recycled capture contains silence for absent input, including after a partial-block flush', () => {
  let Processor
  const captured = []
  const context = vm.createContext({ Float32Array, ArrayBuffer, currentTime: 0,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: (m, transfer) => {
      if (m.type === 'capture') captured.push(structuredClone(m, { transfer }))
    } } } }, registerProcessor: (_, type) => { Processor = type } })
  vm.runInContext(nativeMixerProcessorSource, context)
  const node = new Processor({ processorOptions: { tracks: ['a'], blockFrames: 256, captureOnly: true } })
  const out = [new Float32Array(128), new Float32Array(128)]
  const input = [[new Float32Array(128).fill(.5)]]
  node.port.onmessage({ data: { type: 'arm', startTime: 0 } })
  for (let i = 0; i < 20; i++) {
    node.process(input, [out]); node.process([], [out])
    const message = captured.shift()
    const pcm = new Float32Array(message.samples)
    assert.ok(pcm.slice(0, 256).every(v => v === .5))
    assert.ok(pcm.slice(256).every(v => v === 0))
    node.receiveOutput({ type: 'recycle', samples: message.samples })
  }
  node.process(input, [out])
  node.port.onmessage({ data: { type: 'flush' } })
  node.port.onmessage({ data: { type: 'arm', startTime: 0 } })
  node.process([], [out]); node.process([], [out])
  assert.ok(new Float32Array(captured.shift().samples).every(v => v === 0))
})
