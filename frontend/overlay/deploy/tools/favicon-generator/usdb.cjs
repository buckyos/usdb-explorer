// Derive browser-sized assets from the approved mark during the image build.
// Runtime startup must not fetch our own public origin before the server is up.
const { favicons } = require('favicons');
const fs = require('node:fs/promises');
const path = require('node:path');

async function main() {
  const publicDir = path.resolve(__dirname, '../../../public');
  const source = await fs.readFile(path.join(publicDir, 'assets/usdb/logo.png'));
  const output = path.join(publicDir, 'assets/favicon');
  const result = await favicons(source, {
    path: '/assets/favicon',
    appName: 'USDB Explorer',
    appShortName: 'USDB',
    appDescription: 'USDB network explorer',
    start_url: '/usdb',
    background: '#ffffff',
    theme_color: '#5666d7',
    icons: { android: true, appleIcon: { background: 'transparent' }, appleStartup: false,
      favicons: true, windows: false, yandex: false },
  });
  await fs.mkdir(output, { recursive: true });
  for (const image of result.images) {
    await fs.writeFile(path.join(output, image.name), image.contents);
  }
  for (const file of result.files) {
    await fs.writeFile(path.join(output, file.name), file.contents);
  }
  await fs.copyFile(path.join(output, 'favicon.ico'), path.join(publicDir, 'favicon.ico'));
  await fs.copyFile(path.join(output, 'android-chrome-192x192.png'), path.join(output, 'logo-icon.png'));
  // The default share image is the original logo; retain user-provided OG overrides.
  await fs.mkdir(path.join(publicDir, 'static'), { recursive: true });
  for (const name of ['og_image.png', 'og_placeholder.png']) {
    await fs.writeFile(path.join(publicDir, 'static', name), source);
  }
  console.log('Bundled USDB favicons and share image generated.');
}
main().catch(error => { console.error('USDB branding build failed:', error); process.exitCode = 1; });
