'use strict';

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const mysql = require('mysql2/promise');
const fs = require('fs');
const path = require('path');
require('dotenv').config();

// ── Config ─────────────────────────────────────────────────────────────────
const MEDIA_DIR = path.resolve(
  process.env.WA_MEDIA_DIR || path.join(__dirname, 'media')
);
const GROUP_NAME = process.env.WA_GROUP_NAME || 'Workouts';
const INITIAL_LIMIT = parseInt(process.env.INITIAL_SCRAPE_LIMIT || '100', 10);
const LOG_FILE = path.join(__dirname, 'scraper.log');

if (!fs.existsSync(MEDIA_DIR)) {
  fs.mkdirSync(MEDIA_DIR, { recursive: true });
}

// ── Logger ──────────────────────────────────────────────────────────────────
function log(level, ...args) {
  const ts = new Date().toISOString();
  const line = `[${ts}] [${level}] ${args.join(' ')}`;
  console.log(line);
  try {
    fs.appendFileSync(LOG_FILE, line + '\n');
  } catch (_) {}
}

// ── DB pool (lazy) ──────────────────────────────────────────────────────────
let _pool;
function getPool() {
  if (!_pool) {
    _pool = mysql.createPool({
      host: process.env.DB_HOST || 'localhost',
      user: process.env.DB_USER || 'root',
      password: process.env.DB_PASSWORD || '',
      database: process.env.DB_NAME || 'credentialing',
      port: parseInt(process.env.DB_PORT || '3306', 10),
      ssl: process.env.DB_SSL === 'true' ? { rejectUnauthorized: false } : undefined,
      waitForConnections: true,
      connectionLimit: 5,
      queueLimit: 0,
    });
  }
  return _pool;
}

// ── WhatsApp client ─────────────────────────────────────────────────────────
const client = new Client({
  authStrategy: new LocalAuth({
    clientId: 'usdrowing-wa-scraper',
    dataPath: path.join(__dirname, '.wwebjs_auth'),
  }),
  puppeteer: {
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-accelerated-2d-canvas',
      '--no-first-run',
      '--no-zygote',
      '--disable-gpu',
    ],
    headless: true,
  },
});

// ── Event handlers ──────────────────────────────────────────────────────────
client.on('qr', (qr) => {
  log('INFO', 'Scan this QR code with your WhatsApp phone:');
  qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => log('INFO', 'Session authenticated'));

client.on('auth_failure', (msg) => log('ERROR', 'Auth failure:', msg));

client.on('disconnected', (reason) => {
  log('WARN', 'Client disconnected:', reason);
});

client.on('ready', async () => {
  log('INFO', `Client ready — watching group "${GROUP_NAME}"`);
  if (INITIAL_LIMIT > 0) {
    await scrapeRecent().catch((e) => log('ERROR', 'Initial scrape failed:', e.message));
  }
});

// Fires for every new message received by this account.
// In a group, this includes all members' messages.
client.on('message', async (msg) => {
  if (!msg.hasMedia) return;
  try {
    const chat = await msg.getChat();
    if (!chat.isGroup || chat.name !== GROUP_NAME) return;
    await saveMedia(msg);
  } catch (err) {
    log('ERROR', 'message handler error:', err.message);
  }
});

// ── Core save logic ─────────────────────────────────────────────────────────
async function saveMedia(msg) {
  // Sender: in a group, msg.author is the individual member JID.
  // msg.from is the group JID itself.
  const senderJid = msg.author || msg.from;
  const senderPhone = senderJid.replace(/@c\.us$/, '').replace(/@g\.us$/, '');

  // Stable per-message filename using the serialised message id
  const msgId = msg.id._serialized;
  const safeId = msgId.replace(/[^a-zA-Z0-9_-]/g, '_');

  // Deduplicate: skip if we already stored this message id
  const db = getPool();
  const [existing] = await db.execute(
    'SELECT id FROM pending_whatsapp_scans WHERE wa_message_id = ?',
    [msgId]
  );
  if (existing.length > 0) return; // already stored

  const media = await msg.downloadMedia();
  if (!media) return;

  const mimeType = media.mimetype || '';
  if (!mimeType.startsWith('image/')) return; // only images

  const ext = mimeType.split('/')[1].split(';')[0] || 'jpg';
  const filename = `${safeId}.${ext}`;
  const filepath = path.join(MEDIA_DIR, filename);

  fs.writeFileSync(filepath, Buffer.from(media.data, 'base64'));

  // WhatsApp timestamp is Unix seconds
  const receivedAt = new Date(msg.timestamp * 1000)
    .toISOString()
    .slice(0, 19)
    .replace('T', ' ');

  await db.execute(
    `INSERT INTO pending_whatsapp_scans
       (image_path, sender_phone, received_at, wa_message_id)
     VALUES (?, ?, ?, ?)`,
    [filepath, senderPhone, receivedAt, msgId]
  );

  log('INFO', `Saved image from ${senderPhone} → ${filename}`);
}

// ── Initial backfill ────────────────────────────────────────────────────────
async function scrapeRecent() {
  log('INFO', `Back-filling up to ${INITIAL_LIMIT} messages from "${GROUP_NAME}"...`);
  const chats = await client.getChats();
  const group = chats.find((c) => c.isGroup && c.name === GROUP_NAME);
  if (!group) {
    log('WARN', `Group "${GROUP_NAME}" not found. Check WA_GROUP_NAME in .env`);
    return;
  }

  const messages = await group.fetchMessages({ limit: INITIAL_LIMIT });
  log('INFO', `Found ${messages.length} messages, processing images...`);

  let saved = 0;
  let skipped = 0;
  for (const msg of messages) {
    if (!msg.hasMedia) continue;
    try {
      await saveMedia(msg);
      saved++;
    } catch (err) {
      log('WARN', `Could not save message ${msg.id._serialized}:`, err.message);
      skipped++;
    }
  }
  log('INFO', `Back-fill complete — ${saved} saved, ${skipped} skipped`);
}

// ── Start ───────────────────────────────────────────────────────────────────
log('INFO', `Starting WhatsApp scraper for group "${GROUP_NAME}"`);
log('INFO', `Media directory: ${MEDIA_DIR}`);
client.initialize();
