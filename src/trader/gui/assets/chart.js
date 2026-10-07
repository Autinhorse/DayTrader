// 图表页面：由 Python（trader.gui.chart）通过 runJavaScript 调用 window.api，
// 通过 QWebChannel 的 bridge 对象回报用户操作（向左拖到头时请求更早的数据）。
// 时间一律是“纽约当地时间的秒数”（Python 已换算），图上显示的就是美东时间。
(function () {
  const LWC = LightweightCharts;
  const el = document.getElementById("chart");
  const legend = document.getElementById("legend");
  const empty = document.getElementById("empty");
  let chart = null;
  let candle = null;
  let bridge = null;
  let mode = "price";
  let seriesInfo = []; // {series, label, key}
  let loadingMore = false;
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
    chart.subscribeCrosshairMove(onCrosshair);
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
  }

  function fmt(v) {
    if (v === undefined || v === null || Number.isNaN(v)) return "-";
    const a = Math.abs(v);
    return a >= 1000 ? v.toFixed(2) : a >= 1 ? v.toFixed(2) : v.toFixed(4);
  }

  function onCrosshair(param) {
    if (!param || !param.time) { legend.innerHTML = window._title || ""; return; }
    let html = (window._title || "") + "<br>";
    if (candle) {
      const c = param.seriesData.get(candle);
      if (c) html += `<span class="t">开</span> ${fmt(c.open)} <span class="t">高</span> ${fmt(c.high)} ` +
                     `<span class="t">低</span> ${fmt(c.low)} <span class="t">收</span> ${fmt(c.close)}<br>`;
    }
    const parts = [];
    for (const s of seriesInfo) {
      const d = param.seriesData.get(s.series);
      if (d && d.value !== undefined) parts.push(`<span class="t">${s.label}</span> ${fmt(d.value)}`);
    }
    legend.innerHTML = html + parts.join("　");
  }

  function onRange(range) {
    if (!range || !bridge || loadingMore || mode !== "price") return;
    if (range.from < 10) { // 拖到最左边：向 Python 要更早的数据
      loadingMore = true;
      bridge.needMore();
    }
  }

  function lineOpts(o, color) {
    const opts = { color: color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
                   crosshairMarkerVisible: false };
    if (o.plot === "band") opts.lineStyle = LWC.LineStyle.Dashed;
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
      let pane = 1;
      if (p.volume && p.volume.length) {
        const vol = chart.addSeries(LWC.HistogramSeries, {
          priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false,
        }, pane);
        vol.setData(p.volume);
        seriesInfo.push({ series: vol, label: "量" });
        pane += 1;
      }
      let ci = 0;
      const markers = (p.markers || []).slice();
      for (const ind of p.indicators || []) {
        let indPane = null;
        for (const o of ind.outputs) {
          const color = o.color || palette[ci++ % palette.length];
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
          seriesInfo.push({ series: s, label: label });
        }
      }
      markers.sort((a, b) => a.time - b.time);
      LWC.createSeriesMarkers(candle, markers);
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
