const express = require('express');
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');
const pino = require('pino');
const qrcode = require('qrcode');
const fs = require('fs');

const app = express();
app.use(express.json());

const PORT = 8732;
let sock = null;
let currentConnectionState = 'connecting';
let currentQrDataUrl = null;

async function connectToWhatsApp() {
    const dataDir = process.env.PROGRAMDATA ? `${process.env.PROGRAMDATA}\\WAPrinter\\baileys_auth_info` : 'baileys_auth_info';
    const { state, saveCreds } = await useMultiFileAuthState(dataDir);
    
    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        logger: pino({ level: 'silent' }) // suppress excessive logging
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update;
        
        if (qr) {
            console.log('\n--- NEW QR CODE GENERATED ---');
            try {
                currentQrDataUrl = await qrcode.toDataURL(qr);
            } catch (err) {
                console.error('Failed to generate QR data URL', err);
            }
        }
        
        if (connection === 'close') {
            currentConnectionState = 'close';
            currentQrDataUrl = null;
            const shouldReconnect = lastDisconnect.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log('connection closed due to ', lastDisconnect.error, ', reconnecting ', shouldReconnect);
            
            if (shouldReconnect) {
                currentConnectionState = 'connecting';
                connectToWhatsApp();
            } else {
                console.log('You are logged out. Please delete the baileys_auth_info folder and restart.');
            }
        } else if (connection === 'open') {
            currentConnectionState = 'open';
            currentQrDataUrl = null;
            console.log('Opened connection to WhatsApp');
        }
    });
}

// Ensure WhatsApp connection starts
connectToWhatsApp();

app.get('/status', (req, res) => {
    res.json({
        state: currentConnectionState,
        qr: currentQrDataUrl
    });
});

app.post('/send', async (req, res) => {
    if (!sock) {
        return res.status(500).json({ error: 'WhatsApp socket not initialized' });
    }
    
    const { recipient, pdf_path, message } = req.body;
    
    if (!recipient || !pdf_path) {
        return res.status(400).json({ error: 'recipient and pdf_path are required' });
    }

    try {
        // Format the number to WhatsApp format
        const waId = `${recipient}@s.whatsapp.net`;
        
        // Ensure file exists
        if (!fs.existsSync(pdf_path)) {
            return res.status(404).json({ error: `File not found: ${pdf_path}` });
        }

        const buffer = fs.readFileSync(pdf_path);
        
        // Extract filename from path
        const filename = pdf_path.split(/[\/\\]/).pop();

        // Send the document first
        const sentMsg = await sock.sendMessage(waId, {
            document: buffer,
            mimetype: 'application/pdf',
            fileName: filename,
            caption: message || '' // You can also send the text as a separate message if you prefer
        });

        res.json({ ok: true, wamid: sentMsg.key.id });
        
    } catch (error) {
        console.error('Error sending message:', error);
        res.status(500).json({ error: error.message });
    }
});

app.listen(PORT, () => {
    console.log(`Baileys Service listening on port ${PORT}`);
});
