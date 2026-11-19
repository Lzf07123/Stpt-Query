/* edu-query-app · Service Worker（PWA 离线兜底）
 *
 * 策略：
 * - 只处理同源 GET；以下路径一律不拦截（动态数据 / 隐私 / 运维面）：
 *   /api/、/cal/、/jump、/get_schedule、/health、/metrics、/admin、/brand-env.js
 *   （brand-env.js 由容器 entrypoint 按环境变量生成，绝不能缓存）
 * - 页面导航：网络优先，失败回退缓存的 index.html（离线兜底）
 * - 静态资源：stale-while-revalidate；**缓存键 = 完整 URL（含 ?v=N）**——
 *   版本号变化即换新键，发版后首次请求就能拿到新资产。
 *   （v1 曾按「去参后的路径」匹配，导致发版后首次刷新永远回上一代旧
 *   资产、版本号失去穿透力——实测反馈后废弃）
 * - 非静态未知路径：直连网络，不做缓存（保持最小惊讶）
 */
"use strict";

const VERSION = "v24";
const CACHE_NAME = "eduquery-shell-" + VERSION;

// 安装期预缓存（路径会解析为完整 URL 存储；页面引用均带 ?v=N，运行期按完整 URL 命中）
const STATIC_PATHS = [
  "/index.html", "/manifest.json",
  "/style.css", "/app.js", "/theme-init.js",
  "/brand.js", "/notice.js", "/health-status.js",
  "/image/icon.svg", "/image/icon-192.png", "/image/icon-512.png",
  "/image/icon-maskable-192.png", "/image/icon-maskable-512.png",
  "/image/apple-touch-icon.png", "/image/gongan.png", "/image/icp.ico"
];

const BYPASS_PREFIXES = [
  "/api/", "/cal/", "/jump", "/get_schedule", "/health",
  "/metrics", "/admin", "/brand-env.js"
];

function isBypassed(url) {
  return BYPASS_PREFIXES.some(function (prefix) { return url.pathname.startsWith(prefix); });
}

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(STATIC_PATHS);
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        return key === CACHE_NAME ? null : caches.delete(key);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isBypassed(url)) return;

  // 页面导航：网络优先，离线回退缓存壳；缓存统一存 /index.html
  //（导航 URL 带 ?option= 等查询参数，逐个存键会无界增长，页面内容与参数无关）
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).then(function (response) {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(function (cache) {
            cache.put("/index.html", copy);
          });
        }
        return response;
      }).catch(function () {
        return caches.match("/index.html").then(function (cached) {
          return cached || Response.error();
        });
      })
    );
    return;
  }

  // 其余静态资源：stale-while-revalidate，键 = 完整 URL（含版本号）
  event.respondWith(
    caches.match(request).then(function (cached) {
      const network = fetch(request).then(function (response) {
        if (response && response.ok && response.type === "basic") {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(function (cache) {
            cache.put(request, copy);
          });
        }
        return response;
      }).catch(function () {
        return cached || Response.error();
      });
      return cached || network;
    })
  );
});
