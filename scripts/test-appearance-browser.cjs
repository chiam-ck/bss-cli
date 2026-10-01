/* Optional real-browser gate. BSS_PLAYWRIGHT_MODULE can point to an existing
   Playwright installation; production portals have no npm dependency. */
const { chromium } = require(process.env.BSS_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const palettes = fs.readFileSync(process.env.BSS_PALETTE_FIXTURE || '/tmp/theme-palettes.tsv', 'utf8').trim().split('\n').map(row => row.split('\t'));
const shared = fs.readFileSync('crates/bss-portal-ui/assets/static/css/portal_base.css', 'utf8');
const controller = fs.readFileSync('crates/bss-portal-ui/assets/static/js/appearance.js', 'utf8');
const picker = fs.readFileSync('crates/bss-portal-ui/assets/templates/partials/appearance_picker.html', 'utf8');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const app of ['self-serve', 'csr']) {
      const css = fs.readFileSync(`portals/${app}/assets/static/css/${app === 'csr' ? 'csr' : 'portal'}.css`, 'utf8');
      for (const [id, palette] of palettes) {
        const context = await browser.newContext({ colorScheme: 'light' });
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', e => errors.push(e.message));
        await page.route('http://theme.test/**', route => route.fulfill({ contentType: 'text/html', body: `<!doctype html><html data-appearance-default="system"><head><style>${shared}${css}${palette}</style><script>${controller}</script></head><body>${app === "csr" ? `<header class="cockpit-header"><div class="cockpit-header-left">Brand</div><nav class="cockpit-header-nav"><a class="cockpit-nav-link">Sessions</a>${picker}</nav><div class="cockpit-header-right">Model</div></header>` : `<header class="portal-header"><div class="brand">Brand</div><nav class="portal-nav">${picker}</nav></header>`}<section class="line-card"><h1>${app} ${id}</h1><input placeholder="Example input"><span class="line-card-state line-card-state--blocked">Blocked</span><button class="btn-danger cancel-confirm-button">Cancel</button><a href="/">Example link</a><p class="chat-bubble chat-bubble-user">User message</p><p class="chat-bubble chat-bubble-assistant">Assistant message</p><div class="line-card-pending-banner">Pending activation</div><div class="line-card--blocked">Service blocked</div><div id="fragment"></div></section></body></html>` }));
        await page.goto('http://theme.test/');
        const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
        assert.equal(await background(), 'rgb(248, 250, 252)');
        await page.emulateMedia({ colorScheme: 'dark' });
        assert.notEqual(await background(), 'rgb(248, 250, 252)');
        await page.selectOption('[data-appearance-picker]', 'light');
        await page.reload();
        assert.equal(await background(), 'rgb(248, 250, 252)');
        assert.equal(await page.locator('[data-appearance-picker]').inputValue(), 'light');
        await page.evaluate(() => { document.querySelector('#fragment').innerHTML = '<div class="chat-bubble chat-bubble-assistant">Streamed fragment</div>'; });
        assert.equal(await page.locator('#fragment > div').evaluate(el => getComputedStyle(el).color), 'rgb(23, 33, 43)');
        for (const mode of ['light', 'dark']) {
          await page.selectOption('[data-appearance-picker]', mode);
          const ratio = await page.locator('.cancel-confirm-button').evaluate(el => {
            const style = getComputedStyle(el);
            function luminance(rgb) {
              const values = rgb.match(/[\d.]+/g).slice(0, 3).map(n => {
                const c = Number(n) / 255;
                return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
              });
              return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
            }
            const fg = luminance(style.color), bg = luminance(style.backgroundColor);
            return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
          });
          assert.ok(ratio >= 4.5, `${app}/${id}/${mode} danger contrast: ${ratio}`);
        }
        await page.selectOption('[data-appearance-picker]', 'dark');
        await page.emulateMedia({ colorScheme: 'light' });
        assert.notEqual(await background(), 'rgb(248, 250, 252)');
        await page.selectOption('[data-appearance-picker]', 'system');
        assert.equal(await background(), 'rgb(248, 250, 252)');
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.locator('[data-appearance-picker]').isVisible());
        if (process.env.BSS_THEME_SCREENSHOTS && id === 'phosphor') {
          await page.screenshot({ path: `${process.env.BSS_THEME_SCREENSHOTS}/${app}-light.png` });
          await page.selectOption('[data-appearance-picker]', 'dark');
          await page.screenshot({ path: `${process.env.BSS_THEME_SCREENSHOTS}/${app}-dark.png` });
        }
        assert.deepEqual(errors, []);
        await context.close();
        console.log(`PASS ${app}/${id}: OS, override, reload, fragments, mobile`);
      }
    }
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
