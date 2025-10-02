#!/usr/bin/env node
import fs from 'fs';

const args = process.argv.slice(2);
if (args.length === 0) {
  console.error('Usage: node hackclub_ai.js <prompt or filepath> [extra instructions]');
  process.exit(1);
}

let prompt;
const firstArg = args[0];

if (fs.existsSync(firstArg)) {
  // If the first arg is a file, read it
  const code = fs.readFileSync(firstArg, 'utf8');
  const extra = args.slice(1).join(' ');
  prompt = `Here is my file:\n\n${code}\n\n${extra}`;
} else {
  // Otherwise treat args as plain prompt
  prompt = args.join(' ');
}

const API = 'https://ai.hackclub.com/chat/completions';
const body = { messages: [{ role: 'user', content: prompt }] };

(async () => {
  try {
    const res = await fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    const data = await res.json();
    const reply = data.choices?.[0]?.message?.content ?? '[no reply]';
    console.log(reply.trim());
  } catch (err) {
    console.error('Request failed:', err.message ?? err);
    process.exit(1);
  }
})();
