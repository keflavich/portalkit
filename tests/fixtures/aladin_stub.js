/*
 * Aladin Lite v3 test stub (ARCHITECTURE.md section 13, Level 1).
 *
 * Served by the e2e harness in place of the CDN / mirror script. It is a classic script that
 * defines window.A with the API surface portalkit uses:
 *   A.init, A.aladin(), setImageSurvey / setBaseImageLayer, newImageSurvey, createImageSurvey,
 *   A.HiPS, A.catalog, A.marker, A.source, A.polygon, A.polyline, A.circle, A.graphicOverlay,
 *   on, gotoRaDec, setFoV / setFov, getRaDec, getFov, pix2world, world2pix, createImageFITS,
 *   setOverlayImageLayer, getOverlayImageLayer, removeImageLayer, addCatalog, addOverlay,
 *   removeOverlay, removeLayer, removeLayers.
 * Every API call is appended to window.__aladinCalls as a [fn, args] pair with JSON-safe
 * arguments (the same layout as the inline fallback stub in tests/e2e/conftest.py). Methods of
 * image layers record the numeric value first: layer.setOpacity -> [alpha, layerName].
 * createImageFITS follows Aladin Lite v3: its success / error callback runs only after the
 * returned image has been passed to setOverlayImageLayer (Image._addToView).
 *
 * window.__aladinStub exposes the state for assertions:
 *   instances            Aladin instances in creation order
 *   summary(k = 0)       JSON-safe view of instance k: view, base, image layers (with opacity),
 *                        catalogs (name, color, source count, shown, idx of sources) and
 *                        graphic overlays (name, shown, item kinds)
 *   fire(name, ...args)  dispatch an Aladin event on instance 0 (e.g. 'positionChanged')
 *   clickSource(catName, k, k2 = 0)  fire objectClicked with source k of the named catalog
 *   hoverSource(catName, k)          fire objectHovered with that source
 *
 * Configuration before the script loads, through window.__aladinStubConfig:
 *   {noOpacity: true}   image layers expose no opacity API (exercises the step fallback)
 *   {fitsFail: true}    createImageFITS calls its error callback
 *   {initDelayMs: n}    A.init resolves after n ms
 */
(function () {
  'use strict';
  var cfg = window.__aladinStubConfig || {};
  var calls = window.__aladinCalls = window.__aladinCalls || [];
  var D2R = Math.PI / 180;

  function safe(v, depth) {
    depth = depth || 0;
    if (v === null || v === undefined) return v === undefined ? null : v;
    var t = typeof v;
    if (t === 'number') return Number.isFinite(v) ? v : String(v);
    if (t === 'string' || t === 'boolean') return v;
    if (t === 'function') return '[function]';
    if (typeof Element !== 'undefined' && v instanceof Element) return '[element ' + v.tagName.toLowerCase() + ']';
    if (v && v.__stubType) return {type: v.__stubType, name: v.name || null};
    if (depth > 3) return '[deep]';
    if (Array.isArray(v) || ArrayBuffer.isView(v)) {
      var out = [];
      var n = Math.min(v.length, 50);
      for (var i = 0; i < n; i++) out.push(safe(v[i], depth + 1));
      if (v.length > n) out.push('[+' + (v.length - n) + ']');
      return out;
    }
    if (t === 'object') {
      var o = {};
      Object.keys(v).slice(0, 40).forEach(function (k) { o[k] = safe(v[k], depth + 1); });
      return o;
    }
    return String(v);
  }

  function rec(fn, args) {
    calls.push([fn, Array.prototype.slice.call(args).map(function (a) { return safe(a); })]);
  }

  // ------------------------------------------------------------------ image layers

  function ImageLayer(url, name, opts) {
    this.__stubType = 'image';
    this.id = url;
    this.url = url;
    this.name = name || url;
    this.options = Object.assign({}, opts || {});
    this.opacity = this.options.opacity !== undefined ? +this.options.opacity : 1;
    this.added = false;
  }
  if (!cfg.noOpacity) {
    ImageLayer.prototype.setOpacity = function (a) { rec('layer.setOpacity', [a, this.name]); this.opacity = +a; };
    ImageLayer.prototype.getOpacity = function () { return this.opacity; };
    ImageLayer.prototype.setOptions = function (o) {
      rec('layer.setOptions', [o, this.name]);
      Object.assign(this.options, o || {});
      if (o && o.opacity !== undefined) this.opacity = +o.opacity;
    };
  }

  function makeImage(url, name, opts) {
    var L = new ImageLayer(url, name, opts);
    if (cfg.noOpacity) {
      // no opacity property either: the layer only knows its url/name
      delete L.opacity;
    }
    return L;
  }

  // ------------------------------------------------------------------ catalogs and overlays

  function Source(ra, dec, data, options) {
    this.__stubType = 'source';
    this.ra = +ra;
    this.dec = +dec;
    this.data = data || {};
    this.options = options || {};
    this.catalog = null;
  }

  function Catalog(opts) {
    this.__stubType = 'catalog';
    this.options = Object.assign({}, opts || {});
    this.name = this.options.name || 'catalog';
    this.color = this.options.color || null;
    this.sourceSize = this.options.sourceSize || 8;
    this.shape = this.options.shape || 'square';
    this.sources = [];
    this.isShowing = true;
    this.view = null;
  }
  Catalog.prototype.addSources = function (s) {
    var arr = Array.isArray(s) ? s : [s];
    rec('catalog.addSources', [this.name, arr.length]);
    for (var i = 0; i < arr.length; i++) { arr[i].catalog = this; this.sources.push(arr[i]); }
  };
  Catalog.prototype.removeAll = function () { rec('catalog.removeAll', [this.name]); this.sources = []; };
  Catalog.prototype.hide = function () { rec('catalog.hide', [this.name]); this.isShowing = false; };
  Catalog.prototype.show = function () { rec('catalog.show', [this.name]); this.isShowing = true; };
  Catalog.prototype.updateShape = function (o) {
    rec('catalog.updateShape', [this.name, o]);
    if (o && o.color) this.color = o.color;
    if (o && o.sourceSize) this.sourceSize = o.sourceSize;
  };
  Catalog.prototype.setColor = function (c) { rec('catalog.setColor', [this.name, c]); this.color = c; };
  Catalog.prototype.setSourceSize = function (s) { rec('catalog.setSourceSize', [this.name, s]); this.sourceSize = s; };

  function Shape(kind, data, opts) {
    this.__stubType = kind;
    this.kind = kind;
    this.data = data;
    this.options = Object.assign({}, opts || {});
    this.name = this.options.name || null;
  }

  function Overlay(opts) {
    this.__stubType = 'overlay';
    this.options = Object.assign({}, opts || {});
    this.name = this.options.name || 'overlay';
    this.color = this.options.color || null;
    this.overlays = [];
    this.isShowing = true;
  }
  Overlay.prototype.add = function (o) {
    rec('overlay.add', [this.name, o && o.kind]);
    this.overlays.push(o);
  };
  Overlay.prototype.addFootprints = function (arr) {
    rec('overlay.addFootprints', [this.name, (arr || []).length]);
    for (var i = 0; i < (arr || []).length; i++) this.overlays.push(arr[i]);
  };
  Overlay.prototype.removeAll = function () { rec('overlay.removeAll', [this.name]); this.overlays = []; };
  Overlay.prototype.hide = function () { rec('overlay.hide', [this.name]); this.isShowing = false; };
  Overlay.prototype.show = function () { rec('overlay.show', [this.name]); this.isShowing = true; };

  // ------------------------------------------------------------------ Aladin instance

  function Aladin(div, opts) {
    this.__stubType = 'aladin';
    this.div = div;
    this.options = Object.assign({}, opts || {});
    var tgt = String(this.options.target || '0 0').trim().split(/[\s,]+/).map(Number);
    this.ra = Number.isFinite(tgt[0]) ? tgt[0] : 0;
    this.dec = Number.isFinite(tgt[1]) ? tgt[1] : 0;
    this.fov = +this.options.fov || 1;
    this.base = null;
    this.layers = {};      // name -> ImageLayer (overlay image layers)
    this.catalogs = [];
    this.overlays = [];
    this.handlers = {};
    if (this.options.survey) this.base = makeImage(String(this.options.survey), 'base');
    if (div && div.appendChild) {
      var c = document.createElement('div');
      c.className = 'aladin-stub-view';
      c.style.cssText = 'position:absolute;inset:0;background:#000;';
      if (getComputedStyle(div).position === 'static') div.style.position = 'relative';
      div.appendChild(c);
    }
  }
  var P = Aladin.prototype;

  P._size = function () {
    var w = this.div && this.div.clientWidth ? this.div.clientWidth : 600;
    var h = this.div && this.div.clientHeight ? this.div.clientHeight : 400;
    return [w, h];
  };
  P._emit = function (name) {
    var args = Array.prototype.slice.call(arguments, 1);
    var hs = (this.handlers[name] || []).slice();
    for (var i = 0; i < hs.length; i++) hs[i].apply(null, args);
  };
  P.on = function (name, fn) {
    rec('on', [name]);
    (this.handlers[name] = this.handlers[name] || []).push(fn);
  };
  P.gotoRaDec = function (ra, dec) {
    rec('gotoRaDec', arguments);
    this.ra = +ra;
    this.dec = +dec;
    this._emit('positionChanged', {ra: this.ra, dec: this.dec, dragging: false});
  };
  P.gotoPosition = P.gotoRaDec;
  P.setFoV = function (f) {
    rec('setFoV', arguments);
    this.fov = Math.max(1e-6, Math.min(360, +f));
    this._emit('zoomChanged', this.fov);
  };
  P.setFov = P.setFoV;
  P.getRaDec = function () { rec('getRaDec', arguments); return [this.ra, this.dec]; };
  P.getFov = function () {
    rec('getFov', arguments);
    var s = this._size();
    return [this.fov, this.fov * s[1] / s[0]];
  };
  P.getSize = function () { return this._size(); };
  P._scale = function () { return this.fov / this._size()[0]; };   // deg per px
  P.pix2world = function (x, y) {
    rec('pix2world', arguments);
    var s = this._size(), k = this._scale();
    var dec = this.dec - (y - s[1] / 2) * k;
    var ra = this.ra - (x - s[0] / 2) * k / Math.max(1e-6, Math.cos(this.dec * D2R));
    return [((ra % 360) + 360) % 360, dec];
  };
  P.world2pix = function (ra, dec) {
    rec('world2pix', arguments);
    var s = this._size(), k = this._scale();
    var dra = ra - this.ra;
    if (dra > 180) dra -= 360;
    if (dra < -180) dra += 360;
    return [s[0] / 2 - dra * Math.cos(this.dec * D2R) / k, s[1] / 2 - (dec - this.dec) / k];
  };

  P.setImageSurvey = function (s) {
    rec('setImageSurvey', arguments);
    this.base = typeof s === 'string' ? makeImage(s, 'base') : s;
    return this.base;
  };
  P.setBaseImageLayer = function (s) {
    rec('setBaseImageLayer', arguments);
    this.base = typeof s === 'string' ? makeImage(s, 'base') : s;
    return this.base;
  };
  P.getBaseImageLayer = function () { return this.base; };
  P.newImageSurvey = function (url, opts) {
    rec('newImageSurvey', arguments);
    return makeImage(url, (opts && opts.name) || url, opts);
  };
  P.createImageSurvey = function (id, name, url) {
    rec('createImageSurvey', arguments);
    return makeImage(url || id, name || id);
  };
  P.setOverlayImageLayer = function (layer, name) {
    rec('setOverlayImageLayer', arguments);
    var L = typeof layer === 'string' ? makeImage(layer, name) : layer;
    var nm = name || (L && L.name) || 'overlay';
    L.name = nm;
    L.added = true;
    this.layers[nm] = L;
    if (L.__fitsPending) {
      // v3: Image._addToView loads the FITS file and then runs the callbacks
      var cb = L.__fitsPending, self = this;
      L.__fitsPending = null;
      setTimeout(function () {
        if (cfg.fitsFail) { if (cb.err) cb.err(new Error('stub FITS failure')); return; }
        if (cb.ok) cb.ok(self.ra, self.dec, self.fov, L);
      }, 20);
    }
    return L;
  };
  P.getOverlayImageLayer = function (name) { return this.layers[name] || null; };
  P.removeImageLayer = function (name) {
    rec('removeImageLayer', arguments);
    if (this.layers[name]) { this.layers[name].added = false; delete this.layers[name]; }
  };
  P.createImageFITS = function (url, opts, ok, err) {
    rec('createImageFITS', arguments);
    var L = makeImage(url, (opts && opts.name) || url, opts);
    L.__fitsPending = {ok: ok, err: err};   // fired by setOverlayImageLayer, as in v3
    return L;
  };
  P.addCatalog = function (c) {
    rec('addCatalog', [c && c.name]);
    if (this.catalogs.indexOf(c) < 0) this.catalogs.push(c);
    c.view = this;
  };
  P.addOverlay = function (o) {
    rec('addOverlay', [o && o.name]);
    if (this.overlays.indexOf(o) < 0) this.overlays.push(o);
  };
  P._drop = function (obj) {
    var k = this.catalogs.indexOf(obj);
    if (k >= 0) { this.catalogs.splice(k, 1); return true; }
    k = this.overlays.indexOf(obj);
    if (k >= 0) { this.overlays.splice(k, 1); return true; }
    if (typeof obj === 'string') {
      var self = this, hit = false;
      this.catalogs = this.catalogs.filter(function (c) { if (c.name === obj) { hit = true; return false; } return true; });
      this.overlays = this.overlays.filter(function (c) { if (c.name === obj) { hit = true; return false; } return true; });
      if (self.layers[obj]) { delete self.layers[obj]; hit = true; }
      return hit;
    }
    return false;
  };
  P.removeOverlay = function (o) { rec('removeOverlay', [o && (o.name || o)]); this._drop(o); };
  P.removeLayer = function (o) { rec('removeLayer', [o && (o.name || o)]); this._drop(o); };
  P.removeLayers = function () { rec('removeLayers', []); this.catalogs = []; this.overlays = []; };
  P.removeOverlays = P.removeLayers;
  P.setFrame = function (f) { rec('setFrame', arguments); };
  P.showReticle = function (v) { rec('showReticle', arguments); };
  P.destroy = function () { rec('destroy', []); };

  // ------------------------------------------------------------------ namespace

  var instances = [];
  var A = {
    __stub: true,
    init: new Promise(function (resolve) {
      if (cfg.initDelayMs) setTimeout(resolve, cfg.initDelayMs);
      else resolve();
    }),
    aladin: function (div, opts) {
      rec('aladin', arguments);
      var el = typeof div === 'string' ? document.querySelector(div) : div;
      var a = new Aladin(el, opts);
      instances.push(a);
      return a;
    },
    HiPS: function (url, opts) {
      rec('HiPS', arguments);
      return makeImage(url, (opts && opts.name) || url, opts);
    },
    imageHiPS: function (url, opts) { return A.HiPS(url, opts); },
    catalog: function (opts) { rec('catalog', arguments); return new Catalog(opts); },
    source: function (ra, dec, data, options) { return new Source(ra, dec, data, options); },
    marker: function (ra, dec, options, data) {
      rec('marker', [ra, dec, options]);
      var s = new Source(ra, dec, data, options);
      s.isMarker = true;
      return s;
    },
    polygon: function (pts, opts) { rec('polygon', [pts && pts.length, opts]); return new Shape('polygon', pts, opts); },
    polyline: function (pts, opts) { rec('polyline', [pts && pts.length, opts]); return new Shape('polyline', pts, opts); },
    circle: function (ra, dec, r, opts) { rec('circle', [ra, dec, r, opts]); return new Shape('circle', [ra, dec, r], opts); },
    ellipse: function (ra, dec, a, b, th, opts) { rec('ellipse', arguments); return new Shape('ellipse', [ra, dec, a, b, th], opts); },
    graphicOverlay: function (opts) { rec('graphicOverlay', arguments); return new Overlay(opts); },
  };
  window.A = A;

  function inst(k) { return instances[k || 0] || null; }

  window.__aladinStub = {
    config: cfg,
    instances: instances,
    summary: function (k) {
      var a = inst(k);
      if (!a) return null;
      var layers = {};
      Object.keys(a.layers).forEach(function (nm) {
        var L = a.layers[nm];
        layers[nm] = {url: L.url, opacity: L.opacity === undefined ? null : L.opacity};
      });
      return {
        ra: a.ra, dec: a.dec, fov: a.fov,
        base: a.base ? a.base.url : null,
        layers: layers,
        catalogs: a.catalogs.map(function (c) {
          return {name: c.name, color: c.color, n: c.sources.length, shown: c.isShowing,
            idx: c.sources.slice(0, 5000).map(function (s) { return s.data && s.data.idx !== undefined ? s.data.idx : null; })};
        }),
        overlays: a.overlays.map(function (o) {
          return {name: o.name, shown: o.isShowing, n: o.overlays.length,
            kinds: o.overlays.map(function (s) { return s.kind; })};
        }),
      };
    },
    fire: function (name) {
      var a = inst(0);
      if (a) a._emit.apply(a, arguments);
    },
    fireOn: function (k, name) {
      var a = inst(k);
      if (a) a._emit.apply(a, Array.prototype.slice.call(arguments, 1));
    },
    clickSource: function (catName, k, inst_) {
      var a = inst(inst_ || 0);
      if (!a) return false;
      var c = a.catalogs.filter(function (x) { return x.name === catName; })[0];
      if (!c || !c.sources[k]) return false;
      a._emit('objectClicked', c.sources[k], {x: 10, y: 10});
      return true;
    },
    hoverSource: function (catName, k, inst_) {
      var a = inst(inst_ || 0);
      if (!a) return false;
      var c = a.catalogs.filter(function (x) { return x.name === catName; })[0];
      if (!c || !c.sources[k]) return false;
      a._emit('objectHovered', c.sources[k], {x: 10, y: 10});
      return true;
    },
  };
})();
