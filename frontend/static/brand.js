/* edu-query-app · 品牌单点（Li&Design 槽位 1/3/18 的唯一出处）
   名称/slogan/备案/页脚链接全部从这里读取，页面与其它脚本禁止硬编码品牌文案。 */
(function () {
  "use strict";

  var env = window.EDU_QUERY_BRAND_ENV || {};

  function pick(key, fallback) {
    var value = env[key];
    return typeof value === "string" && value.length > 0 ? value : fallback;
  }

  var github = pick("BRAND_GITHUB", "https://github.com/Lzf07123/STPT-Query");
  if (github === "none") github = "";

  var BRAND = Object.freeze({
    name: pick("BRAND_NAME", "教务信息查询"),
    repo: "edu-query-app",
    techId: "eduquery",
    slogan: pick("BRAND_SLOGAN", "汕职院教务信息查询"),
    description: pick("BRAND_DESCRIPTION", "通过学校统一身份认证查询成绩与课表，支持成绩分析与 PDF 导出。"),
    themeKey: "eduquery-theme",
    author: pick("BRAND_AUTHOR", "Lzf07123"),
    links: {
      github: github
    },
    /* ICP/公安备案上线前留空；禁止写入假备案号 */
    filing: {
      icp: "",
      gongan: ""
    }
  });

  window.EDU_QUERY_BRAND = BRAND;

  function renderFooter() {
    var el = document.getElementById("siteFooterInner");
    if (!el) return;
    var year = new Date().getFullYear();
    var parts = [];
    parts.push("© " + year + " " + BRAND.author);
    if (BRAND.links.github) {
      parts.push(
        '<a href="' + BRAND.links.github + '" target="_blank" rel="noopener noreferrer">GitHub</a>'
      );
    }
    el.innerHTML = parts.join('<span aria-hidden="true"> · </span>');
  }

  function applyBrandSlots() {
    var sub = document.getElementById("brandSub");
    if (sub && BRAND.slogan) sub.textContent = BRAND.slogan;
    var title = document.querySelector(".page-title");
    if (title && BRAND.name) title.textContent = BRAND.name;
    var metaDesc = document.querySelector('meta[name="description"]');
    if (metaDesc && BRAND.description) metaDesc.setAttribute("content", BRAND.description);
    document.title = BRAND.name;
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyBrandSlots();
    renderFooter();
  });
})();
