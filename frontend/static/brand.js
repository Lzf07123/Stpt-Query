/* edu-query-app · 品牌单点（Li&Design 槽位 1/3/18 的唯一出处）
   名称/slogan/备案/页脚链接全部从这里读取，页面与其它脚本禁止硬编码品牌文案。 */
(function () {
  "use strict";

  var BRAND = Object.freeze({
    name: "教务信息查询",
    repo: "edu-query-app",
    techId: "eduquery",
    slogan: "汕职院教务信息查询",
    description: "通过学校统一身份认证查询成绩与课表，支持成绩分析与 PDF 导出。",
    themeKey: "eduquery-theme",
    author: "Lzf07123",
    links: {
      github: "https://github.com/Lzf07123/STPT-Query"
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
    document.title = BRAND.name;
    document.querySelectorAll("[data-brand-name]").forEach(function (el) {
      el.textContent = BRAND.name;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyBrandSlots();
    renderFooter();
  });
})();
