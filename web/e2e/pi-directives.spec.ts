// SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
// SPDX-License-Identifier: AGPL-3.0-or-later

import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

async function runPi(request, text: string) {
  const response = await request.post('/pi/directive', {
    data: { text, workspace_id: 'general' },
  })
  expect(response.ok()).toBeTruthy()
  return response.json()
}

test.describe('pi directives', () => {
  test('plain text routes to the general model workflow', async ({ request }) => {
    const payload = await runPi(request, 'what did I write about DBOS?')
    expect(payload.results).toHaveLength(1)
    expect(payload.results[0].workflow_type).toBe('general_questions')
    expect(payload.results[0].status).toBe('COMPLETED')
  })

  test('start aliases resolve to local model roles', async ({ request }) => {
    for (const alias of ['/start /ocr', '/start /asr', '/start /chandra']) {
      const payload = await runPi(request, alias)
      expect(payload.results).toHaveLength(1)
      expect(payload.results[0].workflow_type).toBe('model_directive')
      expect(payload.results[0].status).toBe('COMPLETED')
    }
  })

  test('bare start names the aliases it needs instead of guessing one', async ({ request }) => {
    const payload = await runPi(request, '/start')
    expect(payload.results[0].workflow_type).toBe('model_directive')
    expect(payload.results[0].status).toBe('FAILED_PERMANENT')
    expect(payload.results[0].help.canonical_examples).toContain('/start /qwen')
  })

  test('new directive tokens chain start directives before a general query', async ({ request }) => {
    const payload = await runPi(
      request,
      '/start /ocr /start /asr /start what owns workflow truth?',
    )
    expect(payload.results.map((result) => result.workflow_type)).toEqual([
      'model_directive',
      'model_directive',
      'model_directive',
      'general_questions',
    ])
  })

  test('get and stop directives are durable model directives', async ({ request }) => {
    const getPayload = await runPi(request, '/get workflowy durable boundary')
    expect(getPayload.results[0].workflow_type).toBe('model_directive')
    expect(getPayload.results[0].status).toBe('COMPLETED')

    const stopPayload = await runPi(request, '/stop /med')
    expect(stopPayload.results[0].workflow_type).toBe('model_directive')
    expect(stopPayload.results[0].status).toBe('COMPLETED')
  })

  test('store directive embeds a local directory', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-store-'))
    fs.writeFileSync(path.join(tempDir, 'note.md'), 'DBOS owns durable workflow state.')
    const payload = await runPi(request, `/start /store ${tempDir}`)
    expect(payload.results[0].workflow_type).toBe('directory_embedding')
    expect(payload.results[0].status).toBe('COMPLETED')
  })

  test('screenshot directive stores image then routes tail text to general model', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-image-'))
    const imagePath = path.join(tempDir, 'screen.png')
    fs.writeFileSync(
      imagePath,
      Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=',
        'base64',
      ),
    )
    const payload = await runPi(request, `/screenshot ${imagePath} what is in this image?`)
    expect(payload.results.map((result) => result.workflow_type)).toEqual([
      'directory_embedding',
      'general_questions',
    ])
  })

  test('start model with image path expands to store plus optional query', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-start-image-'))
    const imagePath = path.join(tempDir, 'whiteboard.png')
    fs.writeFileSync(
      imagePath,
      Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=',
        'base64',
      ),
    )
    const payload = await runPi(request, `/start /ocr ${imagePath} summarize it`)
    expect(payload.results.map((result) => result.workflow_type)).toEqual([
      'model_directive',
      'directory_embedding',
      'general_questions',
    ])
  })

  test('invalid directives return durable failed workflow results', async ({ request }) => {
    const missingStore = await runPi(request, '/start /store')
    expect(missingStore.results[0].workflow_type).toBe('model_directive')
    expect(missingStore.results[0].status).toBe('FAILED_PERMANENT')

    const unknown = await runPi(request, '/frobnicate')
    expect(unknown.results[0].workflow_type).toBe('model_directive')
    expect(unknown.results[0].status).toBe('FAILED_PERMANENT')
  })

  test('failed directives include actionable help', async ({ request }) => {
    const unknown = await runPi(request, '/frobnicate')
    expect(unknown.results[0].help).toBeTruthy()
    expect(unknown.results[0].help.summary).toContain('/frobnicate')
    expect(unknown.results[0].help.canonical_examples.length).toBeGreaterThan(0)

    const missingStore = await runPi(request, '/store')
    expect(missingStore.results[0].help).toBeTruthy()
    expect(missingStore.results[0].help.summary.toLowerCase()).toContain('path')
  })

  test('store with nonexistent path returns failed workflow with help', async ({ request }) => {
    const payload = await runPi(request, '/store /tmp/this-path-does-not-exist-12345')
    expect(payload.results[0].workflow_type).toBe('directory_embedding')
    expect(payload.results[0].status).toBe('FAILED_PERMANENT')
    expect(payload.results[0].help).toBeTruthy()
  })

  test('chained invalid then valid directive runs both', async ({ request }) => {
    const payload = await runPi(request, '/frobnicate /start /ocr')
    expect(payload.results.length).toBeGreaterThanOrEqual(2)
    expect(payload.results[0].status).toBe('FAILED_PERMANENT')
    expect(payload.results[1].status).toBe('COMPLETED')
  })

  test('start with unknown alias returns help with model directory hints', async ({ request }) => {
    const payload = await runPi(request, '/start /not-a-real-alias')
    expect(payload.results[0].status).toBe('FAILED_PERMANENT')
    expect(payload.results[0].help).toBeTruthy()
    expect(payload.results[0].help.summary).toContain('/start')
  })

  test('plain text after start without alias becomes a query against the default model', async ({ request }) => {
    const payload = await runPi(request, '/start what owns workflow truth?')
    expect(payload.results.map((result) => result.workflow_type)).toEqual([
      'model_directive',
      'general_questions',
    ])
  })

  test('embed directive aliases store directive', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-embed-'))
    fs.writeFileSync(path.join(tempDir, 'note.md'), 'Pi has durable boundaries.')
    const payload = await runPi(request, `/embed ${tempDir}`)
    expect(payload.results[0].workflow_type).toBe('directory_embedding')
    expect(payload.results[0].status).toBe('COMPLETED')
  })

  test('store with /remote flag still embeds locally and records the flag', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-remote-'))
    fs.writeFileSync(path.join(tempDir, 'note.md'), 'Remote flag is recorded.')
    const payload = await runPi(request, `/store /remote ${tempDir}`)
    expect(payload.results[0].workflow_type).toBe('directory_embedding')
    expect(payload.results[0].status).toBe('COMPLETED')
  })

  test('send-to-wf text file routes to send_to_workflowy workflow', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-send-to-wf-'))
    const file = path.join(tempDir, 'note.txt')
    fs.writeFileSync(file, 'Pi keeps daily ledger entries.')
    const payload = await runPi(request, `/send-to-wf ${file} 04/28`)
    expect(payload.results[0].workflow_type).toBe('send_to_workflowy')
    expect(payload.results[0].status).toBe('COMPLETED')
  })

  test('send-to-wf image file completes via classifier path', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-send-to-wf-img-'))
    const imagePath = path.join(tempDir, 'screen.png')
    fs.writeFileSync(
      imagePath,
      Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=',
        'base64',
      ),
    )
    const payload = await runPi(request, `/send-to-wf ${imagePath} 11/15`)
    expect(payload.results[0].workflow_type).toBe('send_to_workflowy')
    expect(payload.results[0].status).toBe('COMPLETED')
  })

  test('send-to-wf without month/day returns help', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-send-to-wf-bad-'))
    const file = path.join(tempDir, 'note.txt')
    fs.writeFileSync(file, 'Missing date argument.')
    const payload = await runPi(request, `/send-to-wf ${file}`)
    expect(payload.results[0].status).toBe('FAILED_PERMANENT')
    expect(payload.results[0].help).toBeTruthy()
  })

  test('done directive aggregates embeddings into a recall result', async ({ request }) => {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-done-'))
    fs.writeFileSync(path.join(tempDir, 'a.md'), 'DBOS owns durable workflow boundaries.')
    fs.writeFileSync(path.join(tempDir, 'b.md'), 'Pi orchestrates local agent workflows.')
    await runPi(request, `/store ${tempDir}`)
    const payload = await runPi(request, '/done workflow boundaries')
    expect(payload.results[0].workflow_type).toBe('done_recall')
    expect(payload.results[0].status).toBe('COMPLETED')
  })

  test('done without a query returns help', async ({ request }) => {
    const payload = await runPi(request, '/done')
    expect(payload.results[0].workflow_type).toBe('done_recall')
    expect(payload.results[0].status).toBe('FAILED_PERMANENT')
    expect(payload.results[0].help).toBeTruthy()
  })
})
