const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    recordVideo: { dir: 'videos/', size: { width: 1920, height: 1080 } },
    viewport: { width: 1920, height: 1080 }
  });
  const page = await context.newPage();

  const filePath = `file://${path.resolve(__dirname, 'dashboard.html')}`;
  await page.goto(filePath);
  await page.waitForTimeout(8000);
  await page.click('#classBtn');
  await page.waitForTimeout(28000); 
  await page.waitForTimeout(60000);
  await page.waitForTimeout(25000);
  await page.waitForTimeout(35000);
  await page.click('#classBtn');
  await page.waitForSelector('#confirmEnd', { state: 'visible' });
  await page.waitForTimeout(2000);
  await page.click('#confirmEnd');
  await page.evaluate(() => window.scrollBy(0, 500));
  await page.waitForTimeout(6000);

  const downloadPromise = page.waitForEvent('download');
  await page.click('#exportBtn');
  const download = await downloadPromise;
  await page.waitForTimeout(5000);

  await context.close();
  await browser.close();

  const files = fs.readdirSync('videos');
  const videoFile = files.find(f => f.endsWith('.webm'));
  fs.renameSync(`videos/${videoFile}`, 'raw_recording.webm');
  console.log('Screen recording finished and saved as raw_recording.webm');
})();
