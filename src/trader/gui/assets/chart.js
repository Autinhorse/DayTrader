// 图表页面：由 Python（trader.gui.chart）通过 runJavaScript 调用 window.api，
// 通过 QWebChannel 的 bridge 对象回报用户操作（向左拖到头时请求更早的数据、可见范围变化）。
// 时间一律是“纽约当地时间的秒数”（Python 已换算），图上显示的就是美东时间。
(function () {
  const LWC = LightweightCharts;
  const el = document.getElementById("chart");
  const legend = document.getElementById("legend");
  const empty = document.getElementById("empty");
  const datawin = document.getElementById("datawin");
  const popup = document.getElementById("popup");
  let chart = null;
  let candle = null;
  let bridge = null;
  let mode = "price";
  let seriesInfo = []; // {series, label, key}
  let volSeries = null;
  let indSeries = []; // indSeries[i][key] = series，用于回放时增量更新
  let markerPlugin = null;
  let markerList = [];
  let loadingMore = false;
  // 按时间查数值（数据窗口、弹出信息用）：K 线、成交量、各指标输出
  let times = []; // K 线时间，升序
  let candleMap = new Map();
  let volMap = new Map();
  let groups = []; // [{label, items: [{key, color, map}]}]，groups[i] 对应第 i 个指标
  let dataWindow = false;
  let lastTime = null; // 十字线所在的时间；null 表示鼠标不在图上
  let popupAt = null; // 弹出窗口出现时的鼠标位置
  let lastPrice = null; // 十字线所在的价格
  // 成交量分布：{mode, profiles: [{label, t0, t1, low, size, vol, poc_i, va, poc, vah, val, total}]}
  let profile = { mode: "off", profiles: [] };
  let profileUpdate = null; // 通知图表重画
  let profileLines = [];
  let rangeTimer = null;
  const palette = ["#2962ff", "#ff9800", "#e91e63", "#00bcd4", "#9c27b0", "#8bc34a", "#ffeb3b", "#795548"];

  new QWebChannel(qt.webChannelTransport, function (channel) {
    bridge = channel.objects.bridge;
    if (bridge) bridge.ready();
  });

  function baseOptions() {
    return {
      autoSize: true,
      layout: { background: { color: "#131722" }, textColor: "#d1d4dc", fontSize: 11,
                panes: { separatorColor: "#2a2e39", enableResize: true } },
      grid: { vertLines: { color: "#1e222d" }, horzLines: { color: "#1e222d" } },
      crosshair: { mode: LWC.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#2a2e39" },
      timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: true, rightOffset: 5 },
      localization: { locale: "zh-CN", dateFormat: "yyyy-MM-dd" },
    };
  }

  function reset() {
    if (chart) chart.remove();
    chart = LWC.createChart(el, baseOptions());
    candle = null;
    seriesInfo = [];
    volSeries = null;
    indSeries = [];
    markerPlugin = null;
    markerList = [];
    times = [];
    candleMap = new Map();
    volMap = new Map();
    groups = [];
    profileUpdate = null;
    profileLines = [];
    hidePopup();
    chart.subscribeCrosshairMove(onCrosshair);
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
  }

  function fmt(v) {
    if (v === undefined || v === null || Number.isNaN(v)) return "-";
    const a = Math.abs(v);
    return a >= 1000 ? v.toFixed(2) : a >= 1 ? v.toFixed(2) : v.toFixed(4);
  }

  function fmtTime(t) {
    // 时间是“纽约当地时间的秒数”，按 UTC 读出来就是纽约时间
    return new Date(t * 1000).toISOString().slice(0, 19).replace("T", " ");
  }

  function fmtVol(v) {
    if (v === undefined || v === null) return "-";
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return v.toFixed(0);
  }

  function esc(x) {
    return String(x).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function row(k, v, cls) {
    return `<div class="r"><span class="k">${k}</span><span class="v ${cls || ""}">${v}</span></div>`;
  }

  function pushTime(t) {
    if (!times.length || t > times[times.length - 1]) times.push(t);
  }

  function indexOfTime(t) {
    let lo = 0, hi = times.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (times[mid] === t) return mid;
      if (times[mid] < t) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
  }

  // 某根 K 线的全部信息（数据窗口和弹出窗口共用）；withMarkers 为真时附上这根 K 线的标记（成交等）
  function infoHtml(t, withMarkers, price) {
    if (t === null || t === undefined) return "";
    const c = candleMap.get(t);
    let html = `<div class="h">${fmtTime(t)}</div>`;
    if (c) {
      const i = indexOfTime(t);
      const prev = i > 0 ? candleMap.get(times[i - 1]) : null;
      const cls = c.close >= c.open ? "up" : "dn";
      html += row("开", fmt(c.open)) + row("高", fmt(c.high)) + row("低", fmt(c.low)) +
              row("收", fmt(c.close), cls);
      if (prev) {
        const d = c.close - prev.close;
        const pct = prev.close ? (d / prev.close) * 100 : 0;
        html += row("涨跌", `${d >= 0 ? "+" : ""}${fmt(d)}（${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%）`,
                    d >= 0 ? "up" : "dn");
      }
      html += row("振幅", fmt(c.high - c.low));
    }
    if (volMap.size) html += row("量", fmtVol(volMap.get(t)));
    for (const g of groups) {
      if (!g.items.length) continue;
      html += `<div class="h" title="${esc(g.label)}">${esc(g.label)}</div>`;
      for (const it of g.items) {
        html += row(`<span class="sw" style="background:${it.color}"></span>${esc(it.key)}`,
                    fmt(it.map.get(t)));
      }
    }
    html += profileHtml(t, price);
    if (withMarkers) {
      const ms = markerList.filter(function (m) { return m.time === t && m.text; });
      if (ms.length) {
        html += `<div class="h">标记</div>`;
        for (const m of ms) html += `<div class="m" style="color:${m.color}">${esc(m.text)}</div>`;
      }
    }
    return html;
  }

  function renderDataWindow() {
    if (!dataWindow) return;
    const t = lastTime !== null ? lastTime : (times.length ? times[times.length - 1] : null);
    datawin.innerHTML = infoHtml(t, false, lastTime !== null ? lastPrice : null);
  }

  function showPopup(t, point, price) {
    const html = infoHtml(t, true, price);
    if (!html) return;
    popup.innerHTML = html;
    popup.style.display = "block";
    const W = window.innerWidth, H = window.innerHeight;
    const w = popup.offsetWidth, h = popup.offsetHeight;
    let x = point.x + 16, y = point.y + 12;
    if (x + w > W - 4) x = Math.max(4, point.x - w - 16);
    if (y + h > H - 4) y = Math.max(4, H - h - 4);
    popup.style.left = x + "px";
    popup.style.top = y + "px";
    popupAt = { x: point.x, y: point.y };
  }

  function hidePopup() {
    popup.style.display = "none";
    popupAt = null;
  }

  // 双击或右键：弹出十字线所在 K 线的信息（右键不显示浏览器菜单）
  function popupHere(e) {
    if (!chart || popup.contains(e.target) || datawin.contains(e.target) || !times.length) return;
    // 按点击位置找 K 线：取画出来的位置离点击处最近的一根（K 线的横坐标随时间递增）
    const rect = el.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) return;
    const ts = chart.timeScale();
    let lo = 0, hi = times.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const cx = ts.timeToCoordinate(times[mid]);
      if (cx !== null && cx < x) lo = mid + 1; else hi = mid;
    }
    let best = lo;
    const c0 = ts.timeToCoordinate(times[lo]);
    const c1 = lo > 0 ? ts.timeToCoordinate(times[lo - 1]) : null;
    if (c1 !== null && c0 !== null && Math.abs(c1 - x) < Math.abs(c0 - x)) best = lo - 1;
    showPopup(times[best], { x: e.clientX, y: e.clientY }, candle ? candle.coordinateToPrice(y) : null);
  }
  document.addEventListener("contextmenu", function (e) { e.preventDefault(); popupHere(e); });
  // 双击自己判断（450 毫秒内、几乎同一位置的两次左键按下），不依赖浏览器的 dblclick 事件
  let lastDown = null;
  document.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    if (popupAt && !popup.contains(e.target)) hidePopup(); // 点击别处关闭
    const now = performance.now();
    if (lastDown && now - lastDown.t < 450 && Math.hypot(e.clientX - lastDown.x, e.clientY - lastDown.y) < 6) {
      lastDown = null;
      popupHere(e);
      return;
    }
    lastDown = { t: now, x: e.clientX, y: e.clientY };
  });
  // 鼠标离开弹出位置一段距离、按 Esc 时关闭（点击别处见上面的 mousedown）
  document.addEventListener("mousemove", function (e) {
    if (popupAt && !popup.contains(e.target) &&
        Math.hypot(e.clientX - popupAt.x, e.clientY - popupAt.y) > 60) hidePopup();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hidePopup(); });

  function onCrosshair(param) {
    const has = param && param.time !== undefined && param.point;
    lastTime = has ? param.time : null;
    lastPrice = has && candle ? candle.coordinateToPrice(param.point.y) : null;
    renderDataWindow();
    if (!has) { legend.innerHTML = window._title || ""; return; }
    let html = (window._title || "") + "<br>";
    if (candle) {
      const c = param.seriesData.get(candle);
      if (c) html += `<span class="t">开</span> ${fmt(c.open)} <span class="t">高</span> ${fmt(c.high)} ` +
                     `<span class="t">低</span> ${fmt(c.low)} <span class="t">收</span> ${fmt(c.close)}<br>`;
    }
    if (dataWindow) { legend.innerHTML = html; return; } // 指标数值在右侧数据窗口
    const parts = [];
    for (const s of seriesInfo) {
      const d = param.seriesData.get(s.series);
      if (d && d.value !== undefined) parts.push(`<span class="t">${s.label}</span> ${fmt(d.value)}`);
    }
    legend.innerHTML = html + parts.join("　");
  }

  function onRange(range) {
    reportRange();
    if (!range || !bridge || loadingMore || mode !== "price") return;
    if (range.from < 10) { // 拖到最左边：向 Python 要更早的数据
      loadingMore = true;
      bridge.needMore();
    }
  }

  // ---------- 成交量分布（横向柱）----------

  const PROFILE_COLORS = { out: "rgba(120,144,156,0.30)", va: "rgba(41,98,255,0.38)",
                           poc: "rgba(239,83,80,0.80)" };

  function firstIndexAtOrAfter(t) {
    let lo = 0, hi = times.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (times[mid] < t) lo = mid + 1; else hi = mid; }
    return lo;
  }

  // 一个分布占的横向区域（像素）：右侧的从右往左长，每日的从当天第一根 K 线往右长
  function profileBox(p, W) {
    if (p.t0 === null || p.t0 === undefined) {
      const w = Math.min(Math.max(W * 0.22, 80), 360);
      return { x: W - w - 2, w: w, right: true };
    }
    const i0 = firstIndexAtOrAfter(p.t0), i1 = firstIndexAtOrAfter(p.t1) - 1;
    if (i0 >= times.length || i1 < i0) return null;
    const ts = chart.timeScale();
    const x0 = ts.logicalToCoordinate(i0), x1 = ts.logicalToCoordinate(i1);
    if (x0 === null || x1 === null || x1 < 0 || x0 > W) return null;
    return { x: x0, w: Math.max(x1 - x0, 6) * 0.95, right: false };
  }

  function drawProfile(ctx, p, W, hr, vr) {
    const box = profileBox(p, W);
    if (!box || !p.vol.length) return;
    let max = 0;
    for (const v of p.vol) if (v > max) max = v;
    if (!max) return;
    for (let i = 0; i < p.vol.length; i++) {
      const v = p.vol[i];
      if (!v) continue;
      // 格子 i 覆盖 low + i × size 起的 size 美元（价位以分为单位，上下各让半分连成一片）
      const lo = p.low + i * p.size - 0.005;
      const yTop = candle.priceToCoordinate(lo + p.size), yBot = candle.priceToCoordinate(lo);
      if (yTop === null || yBot === null) continue;
      const top = Math.round(yTop * vr), bot = Math.round(yBot * vr);
      const h = Math.max(1, bot - top - (bot - top > 3 ? 1 : 0));
      const len = (v / max) * box.w;
      const x = box.right ? box.x + box.w - len : box.x;
      ctx.fillStyle = i === p.poc_i ? PROFILE_COLORS.poc
        : i >= p.va[0] && i <= p.va[1] ? PROFILE_COLORS.va : PROFILE_COLORS.out;
      ctx.fillRect(Math.round(x * hr), top, Math.max(1, Math.round(len * hr)), h);
    }
  }

  const profilePrimitive = {
    attached: function (param) { profileUpdate = param.requestUpdate; },
    detached: function () { profileUpdate = null; },
    updateAllViews: function () {},
    paneViews: function () {
      return [{
        zOrder: function () { return "bottom"; }, // 画在 K 线下面
        renderer: function () {
          return {
            draw: function (target) {
              if (!candle || !profile.profiles.length) return;
              target.useBitmapCoordinateSpace(function (scope) {
                for (const p of profile.profiles) {
                  drawProfile(scope.context, p, scope.mediaSize.width, scope.horizontalPixelRatio,
                              scope.verticalPixelRatio);
                }
              });
            },
          };
        },
      }];
    },
  };

  // 画在右侧的分布：在价格轴上标出 POC、VAH、VAL
  function refreshProfileLines() {
    if (!candle) return;
    for (const l of profileLines) candle.removePriceLine(l);
    profileLines = [];
    const ps = profile.profiles;
    const p = ps.length === 1 && (ps[0].t0 === null || ps[0].t0 === undefined) ? ps[0] : null;
    if (!p) return;
    for (const [title, price, color] of [["POC", p.poc, "#ef5350"], ["VAH", p.vah, "#2962ff"],
                                          ["VAL", p.val, "#2962ff"]]) {
      profileLines.push(candle.createPriceLine({ price: price, color: color, lineVisible: false,
                                                 axisLabelVisible: true, title: title }));
    }
  }

  // 鼠标所在时间、价格对应的分布信息
  function profileHtml(t, price) {
    const ps = profile.profiles;
    if (!ps.length) return "";
    let p = null;
    for (const x of ps) {
      if (x.t0 === null || x.t0 === undefined || (t !== null && t >= x.t0 && t < x.t1)) { p = x; break; }
    }
    if (!p) return "";
    let html = `<div class="h" title="${esc(p.label)}">成交量分布 · ${esc(p.label)}</div>`;
    html += row(`<span class="sw" style="background:${PROFILE_COLORS.poc}"></span>POC`, fmt(p.poc));
    html += row(`<span class="sw" style="background:${PROFILE_COLORS.va}"></span>VAH`, fmt(p.vah));
    html += row(`<span class="sw" style="background:${PROFILE_COLORS.va}"></span>VAL`, fmt(p.val));
    html += row("合计量", fmtVol(p.total));
    if (price !== null && price !== undefined) {
      const i = Math.floor((price - p.low + 0.005) / p.size);
      if (i >= 0 && i < p.vol.length) {
        const lo = p.low + i * p.size;
        html += row(`${fmt(lo)}~${fmt(lo + p.size - 0.01)}`,
                    `${fmtVol(p.vol[i])}（${((p.vol[i] / p.total) * 100).toFixed(1)}%）`);
      }
    }
    return html;
  }

  // 可见范围变化时（停下 0.3 秒后）告诉 Python 第一根和最后一根可见 K 线的时间
  function reportRange() {
    if (!bridge || !chart || profile.mode !== "visible" || !times.length) return;
    clearTimeout(rangeTimer);
    rangeTimer = setTimeout(function () {
      const r = chart.timeScale().getVisibleLogicalRange();
      if (!r) return;
      const a = Math.max(0, Math.min(times.length - 1, Math.ceil(r.from)));
      const b = Math.max(0, Math.min(times.length - 1, Math.floor(r.to)));
      if (b >= a) bridge.visibleRange(times[a], times[b]);
    }, 300);
  }

  function lineOpts(o, color) {
    const opts = { color: color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
                   crosshairMarkerVisible: false };
    if (o.plot === "band" || o.dashed) opts.lineStyle = LWC.LineStyle.Dashed;
    if (o.plot === "hline") opts.lineType = LWC.LineType.WithSteps;
    return opts;
  }

  window.api = {
    // payload: {title, candles, volume, indicators, markers, prepended, range}
    setData: function (p) {
      mode = "price";
      const keep = p.prepended && chart ? chart.timeScale().getVisibleLogicalRange() : null;
      reset();
      window._title = p.title || "";
      legend.innerHTML = window._title;
      empty.style.display = p.candles.length ? "none" : "flex";
      candle = chart.addSeries(LWC.CandlestickSeries, {
        upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
        wickUpColor: "#26a69a", wickDownColor: "#ef5350",
        priceFormat: { type: "price", precision: p.precision || 2, minMove: p.minMove || 0.01 },
      }, 0);
      candle.setData(p.candles);
      for (const c of p.candles) { candleMap.set(c.time, c); pushTime(c.time); }
      candle.attachPrimitive(profilePrimitive);
      let pane = 1;
      if (p.volume && p.volume.length) {
        const vol = chart.addSeries(LWC.HistogramSeries, {
          priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false,
        }, pane);
        vol.setData(p.volume);
        for (const v of p.volume) volMap.set(v.time, v.value);
        volSeries = vol;
        seriesInfo.push({ series: vol, label: "量" });
        pane += 1;
      }
      let ci = 0;
      const markers = (p.markers || []).slice();
      for (const ind of p.indicators || []) {
        let indPane = null;
        const refs = {};
        indSeries.push(refs);
        const group = { label: ind.label, items: [] };
        groups.push(group);
        for (const o of ind.outputs) {
          const color = o.color || palette[ci++ % palette.length];
          const map = new Map();
          for (const d of o.data) map.set(d.time, d.value);
          group.items.push({ key: o.key, color: color, map: map });
          const label = ind.outputs.length > 1 ? `${ind.label}.${o.key}` : ind.label;
          let target = 0;
          if (o.pane === "separate") {
            if (indPane === null) { indPane = pane; pane += 1; }
            target = indPane;
          }
          if (o.plot === "marker") {
            for (const d of o.data) markers.push({ time: d.time, position: "aboveBar", color: color,
                                                   shape: "circle", text: label });
            continue;
          }
          const s = o.plot === "histogram"
            ? chart.addSeries(LWC.HistogramSeries, { color: color, priceLineVisible: false,
                                                     lastValueVisible: false }, target)
            : chart.addSeries(LWC.LineSeries, lineOpts(o, color), target);
          s.setData(o.data);
          refs[o.key] = s;
          seriesInfo.push({ series: s, label: label });
        }
      }
      markers.sort((a, b) => a.time - b.time);
      markerList = markers;
      markerPlugin = LWC.createSeriesMarkers(candle, markers);
      // 主图占大部分高度，成交量和副图按比例分配，窗口变矮时 K 线也看得清
      const panes = chart.panes();
      panes[0].setStretchFactor(panes.length > 2 ? 3 : 4);
      for (let i = 1; i < panes.length; i++) panes[i].setStretchFactor(1);
      if (keep) {
        chart.timeScale().setVisibleLogicalRange({ from: keep.from + p.prepended, to: keep.to + p.prepended });
      } else if (p.range) {
        chart.timeScale().setVisibleRange({ from: p.range[0], to: p.range[1] });
      } else if (p.candles.length) {
        const n = p.candles.length;
        chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 200), to: n + 5 });
      }
      setTimeout(function () { loadingMore = false; }, 300);
      renderDataWindow();
    },

    // 回放增量更新：candles/volume 按时间追加或替换最后一根（形成中的 bar 用同一时间反复更新）；
    // indicators: [{i, key, data}]；markers 为新增的标记。follow 为真时视图跟随最新一根。
    update: function (u) {
      if (!candle) return;
      for (const c of u.candles || []) { candle.update(c); candleMap.set(c.time, c); pushTime(c.time); }
      if (volSeries) for (const v of u.volume || []) { volSeries.update(v); volMap.set(v.time, v.value); }
      for (const it of u.indicators || []) {
        const s = indSeries[it.i] && indSeries[it.i][it.key];
        if (s) for (const d of it.data) s.update(d);
        const g = groups[it.i];
        const item = g && g.items.find(function (x) { return x.key === it.key; });
        if (item) for (const d of it.data) item.map.set(d.time, d.value);
      }
      if (u.markers && u.markers.length && markerPlugin) {
        markerList = markerList.concat(u.markers).sort((a, b) => a.time - b.time);
        markerPlugin.setMarkers(markerList);
      }
      if (u.candles && u.candles.length) empty.style.display = "none";
      if (u.follow) chart.timeScale().scrollToRealTime();
      if (lastTime === null) renderDataWindow();
    },

    // 右侧数据窗口开关
    setDataWindow: function (on) {
      dataWindow = !!on;
      document.body.classList.toggle("dw", dataWindow);
      renderDataWindow();
    },

    // 成交量分布；request 为真时（可见范围模式）回报当前可见范围
    setProfile: function (p) {
      profile = { mode: p.mode || "off", profiles: p.profiles || [] };
      refreshProfileLines();
      if (profileUpdate) profileUpdate();
      if (p.request) reportRange();
      renderDataWindow();
    },

    // 没有更早的数据了
    noMore: function () { loadingMore = true; },

    showRange: function (from, to) {
      if (chart) chart.timeScale().setVisibleRange({ from: from, to: to });
    },

    // 线图模式：权益曲线、对比；series: [{name, color, data, kind: "line"|"histogram", pane}]
    setLines: function (p) {
      mode = "lines";
      reset();
      window._title = p.title || "";
      legend.innerHTML = window._title;
      chart.applyOptions({ timeScale: { secondsVisible: false } });
      let any = false;
      p.series.forEach(function (s, i) {
        const color = s.color || palette[i % palette.length];
        const series = s.kind === "histogram"
          ? chart.addSeries(LWC.HistogramSeries, { color: color, priceLineVisible: false }, s.pane || 0)
          : chart.addSeries(LWC.LineSeries, { color: color, lineWidth: 2, priceLineVisible: false }, s.pane || 0);
        series.setData(s.data);
        seriesInfo.push({ series: series, label: s.name });
        any = any || s.data.length > 0;
      });
      empty.style.display = any ? "none" : "flex";
      const panes = chart.panes();
      panes[0].setStretchFactor(3);
      for (let i = 1; i < panes.length; i++) panes[i].setStretchFactor(1);
      chart.timeScale().fitContent();
    },
  };
})();
