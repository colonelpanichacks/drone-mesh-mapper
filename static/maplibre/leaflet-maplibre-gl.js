/* jshint esversion: 6, -W008, -W030, -W083 */
// Generated from src/leaflet-maplibre-gl.mjs. Run `npm run build` to update.
(function(global, factory) {
	typeof exports === "object" && typeof module !== "undefined" ? factory(exports, require("leaflet"), require("maplibre-gl")) : typeof define === "function" && define.amd ? define([
		"exports",
		"leaflet",
		"maplibre-gl"
	], factory) : (global = typeof globalThis !== "undefined" ? globalThis : global || self, factory(global.MaplibreGLLeaflet = {}, global.L, global.maplibregl));
})(this, function(exports, leaflet, maplibre_gl) {
	Object.defineProperty(exports, "__esModule", { value: true });
	//#region \0rolldown/runtime.js
	var __create = Object.create;
	var __defProp = Object.defineProperty;
	var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
	var __getOwnPropNames = Object.getOwnPropertyNames;
	var __getProtoOf = Object.getPrototypeOf;
	var __hasOwnProp = Object.prototype.hasOwnProperty;
	var __copyProps = (to, from, except, desc) => {
		if (from && typeof from === "object" || typeof from === "function") for (var keys = __getOwnPropNames(from), i = 0, n = keys.length, key; i < n; i++) {
			key = keys[i];
			if (!__hasOwnProp.call(to, key) && key !== except) __defProp(to, key, {
				get: ((k) => from[k]).bind(null, key),
				enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable
			});
		}
		return to;
	};
	var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(isNodeMode || !mod || !mod.__esModule || !__hasOwnProp.call(mod, "default") ? __defProp(target, "default", {
		value: mod,
		enumerable: true
	}) : target, mod));
	//#endregion
	leaflet = __toESM(leaflet, 1);
	maplibre_gl = __toESM(maplibre_gl, 1);
	//#region src/leaflet-maplibre-gl.mjs
	var L = leaflet.default || leaflet;
	var MaplibreGL = L.Layer.extend({
		options: {
			updateInterval: 32,
			padding: .1,
			interactive: false,
			pane: "tilePane"
		},
		initialize: function(options) {
			L.setOptions(this, options);
			this._throttledUpdate = L.Util.throttle(this._update, this.options.updateInterval, this);
		},
		onAdd: function(map) {
			if (!this._container) this._initContainer();
			var paneName = this.getPaneName();
			map.getPane(paneName).appendChild(this._container);
			this._initGL();
			this._offset = this._map.containerPointToLayerPoint([0, 0]);
			if (map.options.zoomAnimation) L.DomEvent.on(map._proxy, L.DomUtil.TRANSITION_END, this._transitionEnd, this);
		},
		onRemove: function(map) {
			if (this._map._proxy && this._map.options.zoomAnimation) L.DomEvent.off(this._map._proxy, L.DomUtil.TRANSITION_END, this._transitionEnd, this);
			var paneName = this.getPaneName();
			map.getPane(paneName).removeChild(this._container);
			this._glMap.remove();
			this._glMap = null;
		},
		getEvents: function() {
			return {
				move: this._throttledUpdate,
				zoomanim: this._animateZoom,
				zoom: this._pinchZoom,
				zoomstart: this._zoomStart,
				zoomend: this._zoomEnd,
				resize: this._resize
			};
		},
		getAttribution: function() {
			if (this.options.attributionControl) return this.options.attributionControl.customAttribution;
			var map = this._glMap;
			if (map && this.options.attributionControl !== false) {
				var style = map.getStyle();
				if (style && style.sources) return Object.keys(style.sources).map(function(sourceId) {
					var source = map.getSource(sourceId);
					return source && typeof source.attribution === "string" ? source.attribution.trim() : null;
				}).filter(Boolean).join(", ");
			}
			return "";
		},
		getMaplibreMap: function() {
			return this._glMap;
		},
		getCanvas: function() {
			return this._glMap.getCanvas();
		},
		getSize: function() {
			return this._map.getSize().multiplyBy(1 + this.options.padding * 2);
		},
		getBounds: function() {
			var halfSize = this.getSize().multiplyBy(.5);
			var center = this._map.latLngToContainerPoint(this._map.getCenter());
			return L.latLngBounds(this._map.containerPointToLatLng(center.subtract(halfSize)), this._map.containerPointToLatLng(center.add(halfSize)));
		},
		getContainer: function() {
			return this._container;
		},
		getPaneName: function() {
			return this._map.getPane(this.options.pane) ? this.options.pane : "tilePane";
		},
		_roundPoint: function(p) {
			return {
				x: Math.round(p.x),
				y: Math.round(p.y)
			};
		},
		_initContainer: function() {
			var container = this._container = L.DomUtil.create("div", "leaflet-gl-layer");
			this._resizeContainer();
			var offset = this._map.getSize().multiplyBy(this.options.padding);
			var topLeft = this._map.containerPointToLayerPoint([0, 0]).subtract(offset);
			L.DomUtil.setPosition(container, this._roundPoint(topLeft));
		},
		_resizeContainer: function() {
			var size = this.getSize();
			this._container.style.width = size.x + "px";
			this._container.style.height = size.y + "px";
		},
		_initGL: function() {
			var center = this._map.getCenter();
			var options = L.extend({}, this.options, {
				container: this._container,
				center: [center.lng, center.lat],
				zoom: this._map.getZoom() - 1,
				attributionControl: false
			});
			this._glMap = new maplibre_gl.Map(options);
			var _map = this._map;
			var _currentAttribution = this.getAttribution();
			var _getAttribution = this.getAttribution.bind(this);
			this._glMap.on("load", function() {
				if (_map && _map.attributionControl) {
					_map.attributionControl.removeAttribution(_currentAttribution);
					_map.attributionControl.addAttribution(_getAttribution());
				}
			});
			if (this._glMap.setTransformConstrain) this._glMap.setTransformConstrain(function(center, zoom) {
				return {
					center,
					zoom
				};
			});
			else {
				var transform = this._glMap.transform;
				var transformProto = Object.getPrototypeOf(transform);
				var latRangeDescriptor = Object.getOwnPropertyDescriptor(transformProto, "latRange");
				if (!latRangeDescriptor || latRangeDescriptor.set || latRangeDescriptor.writable) transform.latRange = null;
				var maxValidLatitudeDescriptor = Object.getOwnPropertyDescriptor(transformProto, "maxValidLatitude");
				if (!maxValidLatitudeDescriptor || maxValidLatitudeDescriptor.set || maxValidLatitudeDescriptor.writable) transform.maxValidLatitude = Infinity;
				if (transform._helper && transform._helper._latRange) transform._helper._latRange = [-Infinity, Infinity];
			}
			this._transformGL(this._glMap);
			if (this._glMap._canvas.canvas) this._glMap._actualCanvas = this._glMap._canvas.canvas;
			else this._glMap._actualCanvas = this._glMap._canvas;
			var canvas = this._glMap._actualCanvas;
			L.DomUtil.addClass(canvas, "leaflet-image-layer");
			L.DomUtil.addClass(canvas, "leaflet-zoom-animated");
			if (this.options.interactive) L.DomUtil.addClass(canvas, "leaflet-interactive");
			if (this.options.className) L.DomUtil.addClass(canvas, this.options.className);
		},
		_update: function(e) {
			if (!this._map) return;
			this._offset = this._map.containerPointToLayerPoint([0, 0]);
			if (this._zooming) return;
			var container = this._container, gl = this._glMap, offset = this._map.getSize().multiplyBy(this.options.padding), topLeft = this._map.containerPointToLayerPoint([0, 0]).subtract(offset);
			L.DomUtil.setPosition(container, this._roundPoint(topLeft));
			this._transformGL(gl);
		},
		_transformGL: function(gl) {
			var center = this._map.getCenter();
			gl.jumpTo({
				center: [center.lng, center.lat],
				zoom: this._map.getZoom() - 1
			});
		},
		_pinchZoom: function(e) {
			this._glMap.jumpTo({
				zoom: this._map.getZoom() - 1,
				center: this._map.getCenter()
			});
		},
		_animateZoom: function(e) {
			var scale = this._map.getZoomScale(e.zoom);
			var padding = this._map.getSize().multiplyBy(this.options.padding * scale);
			var viewHalf = this.getSize()._divideBy(2);
			var topLeft = this._map.project(e.center, e.zoom)._subtract(viewHalf)._add(this._map._getMapPanePos().add(padding))._round();
			var offset = this._map.project(this._map.getBounds().getNorthWest(), e.zoom)._subtract(topLeft);
			L.DomUtil.setTransform(this._glMap._actualCanvas, offset.subtract(this._offset), scale);
		},
		_zoomStart: function(e) {
			this._zooming = true;
		},
		_zoomEnd: function() {
			var scale = this._map.getZoomScale(this._map.getZoom());
			L.DomUtil.setTransform(this._glMap._actualCanvas, null, scale);
			this._zooming = false;
			this._update();
		},
		_transitionEnd: function(e) {
			L.Util.requestAnimFrame(function() {
				var zoom = this._map.getZoom();
				var center = this._map.getCenter();
				var offset = this._map.latLngToContainerPoint(this._map.getBounds().getNorthWest());
				this._resizeContainer();
				L.DomUtil.setTransform(this._glMap._actualCanvas, offset, 1);
				this._glMap.once("moveend", L.Util.bind(function() {
					this._zoomEnd();
				}, this));
				this._glMap.jumpTo({
					center,
					zoom: zoom - 1
				});
			}, this);
		},
		_resize: function(e) {
			this._transitionEnd(e);
		}
	});
	var maplibreGL = function(options) {
		return new MaplibreGL(options);
	};
	if (Object.isExtensible(L)) {
		L.MaplibreGL = MaplibreGL;
		L.maplibreGL = maplibreGL;
	}
	//#endregion
	exports.MaplibreGL = MaplibreGL;
	exports.default = maplibreGL;
	exports.maplibreGL = maplibreGL;
});
