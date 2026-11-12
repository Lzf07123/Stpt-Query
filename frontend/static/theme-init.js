
    (function () {
      var t = null;
      try { t = localStorage.getItem("eduquery-theme"); } catch (e) {}
      if (t !== "dark" && t !== "light") {
        t = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light";
      }
      document.documentElement.classList.toggle("dark", t === "dark");
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", t === "dark" ? "#3A3F45" : "#F6FBF9");
    })();
  
