const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

async function render(svgPath) {
  const svg = fs.readFileSync(svgPath, "utf8");
  const viewBox = svg.match(/viewBox="0 0 ([\d.]+) ([\d.]+)"/);
  if (!viewBox) throw new Error(`Missing viewBox in ${svgPath}`);
  const width = Math.ceil(Number(viewBox[1]));
  const height = Math.ceil(Number(viewBox[2]));
  const browserCandidates = [
    process.env.CHROME_PATH,
    chromium.executablePath(),
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean);
  const executablePath = browserCandidates.find((candidate) => fs.existsSync(candidate));
  if (!executablePath) throw new Error("No Chromium-compatible browser found");
  const browser = await chromium.launch({
    headless: true,
    executablePath,
  });
  const page = await browser.newPage({ viewport: { width, height } });
  await page.setContent(
    `<style>@page{size:${width}px ${height}px;margin:0}` +
      `html,body{margin:0;width:${width}px;height:${height}px;overflow:hidden}` +
      `svg{display:block;width:${width}px;height:${height}px}</style>${svg}`,
  );
  const stem = svgPath.replace(/\.svg$/i, "");
  await page.pdf({
    path: `${stem}.pdf`,
    width: `${width}px`,
    height: `${height}px`,
    printBackground: true,
    margin: { top: "0", right: "0", bottom: "0", left: "0" },
  });
  await page.screenshot({ path: `${stem}.png`, omitBackground: false });
  await browser.close();
}

(async () => {
  for (const input of process.argv.slice(2)) {
    await render(path.resolve(input));
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
