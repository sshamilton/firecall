import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  MapContainer,
  TileLayer,
  Marker,
  Popup,
  useMap,
  Tooltip,
} from 'react-leaflet';
import MarkerClusterGroup from 'react-leaflet-cluster';
import L from 'leaflet';
import {
  Flame,
  Car,
  Bell,
  Wind,
  Trees,
  HeartPulse,
  ShieldAlert,
  Radio,
  Search,
  Volume2,
  Sparkles,
  MapPin,
  Building2,
  RefreshCw,
  Layers,
  Clock,
  CheckCircle2,
  Edit3,
  X,
  Filter,
} from 'lucide-react';

export interface Dispatch {
  id: number;
  timestamp: string;
  agency: string;
  agencies: string[];
  tones: number[];
  message: string;
  raw_message: string;
  channel: string;
  frequency: string;
  audio_file: string;
  audio_url: string;
  audio_stream_url: string | null;
  has_local_audio: boolean;
  total_duration_sec: number;
  active_transmission_sec: number;
  speech_sec: number;
  incident_type: string;
  extracted_address: string | null;
  municipality: string | null;
  cross_streets: string | null;
  latitude: number;
  longitude: number;
  location_precision: 'exact' | 'approximate' | 'station_fallback';
  parse_method: 'llm' | 'regex' | 'regex_fast' | 'manual';
}

export interface Station {
  id: string;
  name: string;
  municipality: string;
  town: string;
  lat: number;
  lon: number;
  address: string;
}

export interface StatsResponse {
  total_all_time: number;
  last_24h: number;
  last_7d: number;
  filtered_total: number;
  by_category: Record<string, number>;
  by_precision: Record<string, number>;
  by_parse_method: Record<string, number>;
  top_agencies: { agency: string; count: number }[];
  categories: string[];
  llm: {
    enabled: boolean;
    model: string;
  };
  map_config?: {
    carto_api_key?: string;
  };
}

const CATEGORY_CONFIG: Record<
  string,
  { color: string; bg: string; border: string; text: string; shortLabel: string; svg: string }
> = {
  'Structure Fire': {
    color: '#ef4444',
    bg: 'bg-red-500/15',
    border: 'border-red-500/40',
    text: 'text-red-400',
    shortLabel: 'Fire',
    svg: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
  },
  MVA: {
    color: '#f97316',
    bg: 'bg-orange-500/15',
    border: 'border-orange-500/40',
    text: 'text-orange-400',
    shortLabel: 'MVA',
    svg: '<path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2"/><circle cx="7" cy="17" r="2"/><path d="M9 17h6"/><circle cx="17" cy="17" r="2"/>',
  },
  'Automatic Alarm': {
    color: '#eab308',
    bg: 'bg-yellow-500/15',
    border: 'border-yellow-500/40',
    text: 'text-yellow-400',
    shortLabel: 'Alarm',
    svg: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  },
  'Gas / CO / Hazmat': {
    color: '#a855f7',
    bg: 'bg-purple-500/15',
    border: 'border-purple-500/40',
    text: 'text-purple-400',
    shortLabel: 'Gas/Hazmat',
    svg: '<path d="M17.7 7.7a2.5 2.5 0 1 1 1.8 4.3H2"/><path d="M9.6 4.6A2 2 0 1 1 11 8H2"/><path d="M12.6 19.4A2 2 0 1 0 14 16H2"/>',
  },
  'Brush / Outside Fire': {
    color: '#22c55e',
    bg: 'bg-green-500/15',
    border: 'border-green-500/40',
    text: 'text-green-400',
    shortLabel: 'Outside/Brush',
    svg: '<path d="m17 14 3 3.3a1 1 0 0 1-.7 1.7H4.7a1 1 0 0 1-.7-1.7L7 14h-.3a1 1 0 0 1-.7-1.7L9 9h-.2A1 1 0 0 1 8 7.3L12 3l4 4.3a1 1 0 0 1-.8 1.7H15l3 3.3a1 1 0 0 1-.7 1.7H17Z"/><path d="M12 22v-3"/>',
  },
  'Medical / EMS': {
    color: '#3b82f6',
    bg: 'bg-blue-500/15',
    border: 'border-blue-500/40',
    text: 'text-blue-400',
    shortLabel: 'EMS/Rescue',
    svg: '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/><path d="M3.22 12H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"/>',
  },
  'Mutual Aid / Standby': {
    color: '#06b6d4',
    bg: 'bg-cyan-500/15',
    border: 'border-cyan-500/40',
    text: 'text-cyan-400',
    shortLabel: 'Mutual Aid',
    svg: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  },
  Other: {
    color: '#64748b',
    bg: 'bg-slate-500/15',
    border: 'border-slate-500/40',
    text: 'text-slate-300',
    shortLabel: 'Other',
    svg: '<circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.49m-8.48-.01a6 6 0 0 1 0-8.49m11.31-2.82a10 10 0 0 1 0 14.14m-14.14 0a10 10 0 0 1 0-14.14"/>',
  },
};

const MAP_TILES: Record<string, { name: string; url: string; attribution: string }> = {
  dark: {
    name: 'Dark CAD',
    url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
  },
  osm: {
    name: 'OpenStreetMap',
    url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: '&copy; OpenStreetMap contributors',
  },
  light: {
    name: 'Light Positron',
    url: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
  },
};

const ICON_CACHE: Record<string, L.DivIcon> = {};

function createIncidentIcon(
  category: string,
  _isSelected: boolean,
  isRecent: boolean,
  precision: string
) {
  const cacheKey = `${category}:${isRecent ? '1' : '0'}:${precision}`;
  if (ICON_CACHE[cacheKey]) {
    return ICON_CACHE[cacheKey];
  }
  const cfg = CATEGORY_CONFIG[category] || CATEGORY_CONFIG.Other;
  const size = 30;
  const isFallback = precision === 'station_fallback';
  const borderStyle = isFallback ? '2px dashed rgba(255,255,255,0.85)' : '2.5px solid #ffffff';
  const ringShadow = `0 3px 10px rgba(0,0,0,0.6)`;

  const html = `
    <div class="relative flex items-center justify-center ${isRecent ? 'pin-pulse' : ''}"
         style="width:${size}px;height:${size}px;background:${cfg.color};border:${borderStyle};border-radius:9999px;box-shadow:${ringShadow};transition:all 0.15s ease;">
      <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
        ${cfg.svg}
      </svg>
    </div>
  `;

  const icon = L.divIcon({
    html,
    className: 'custom-incident-pin',
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -size / 2],
  });
  ICON_CACHE[cacheKey] = icon;
  return icon;
}

const stationIcon = L.divIcon({
  html: `
    <div style="width:22px;height:22px;background:#1e293b;border:2px solid #94a3b8;border-radius:6px;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 6px rgba(0,0,0,0.6);">
      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#f8fafc" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M9 21v-6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v6"/>
      </svg>
    </div>
  `,
  className: 'custom-station-pin',
  iconSize: [22, 22],
  iconAnchor: [11, 11],
  popupAnchor: [0, -11],
});

function formatTimestamp(ts: string): { relative: string; localFormatted: string; isRecent: boolean } {
  const iso = ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z';
  const date = new Date(iso);
  if (isNaN(date.getTime())) {
    return { relative: ts, localFormatted: ts, isRecent: false };
  }
  const diffSec = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  const isRecent = diffSec < 3600; // within last hour
  let relative = '';
  if (diffSec < 60) relative = 'Just now';
  else if (diffSec < 3600) relative = `${Math.floor(diffSec / 60)}m ago`;
  else if (diffSec < 86400) relative = `${Math.floor(diffSec / 3600)}h ${Math.floor((diffSec % 3600) / 60)}m ago`;
  else relative = `${Math.floor(diffSec / 86400)}d ago`;

  const localFormatted = date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
  return { relative, localFormatted, isRecent };
}

function MapFlyToController({
  flyRequest,
  markerRefs,
  mobileView,
}: {
  flyRequest: { id: number; lat: number; lon: number; seq: number } | null;
  markerRefs: React.MutableRefObject<Record<number, L.Marker | null>>;
  mobileView: 'split' | 'map' | 'list';
}) {
  const map = useMap();

  useEffect(() => {
    const t = setTimeout(() => {
      map.invalidateSize();
    }, 100);
    return () => clearTimeout(t);
  }, [mobileView, map]);

  useEffect(() => {
    if (!flyRequest) return;
    map.invalidateSize();
    map.flyTo([flyRequest.lat, flyRequest.lon], Math.max(map.getZoom(), 14), {
      duration: 0.75,
    });
    const timer = setTimeout(() => {
      const marker = markerRefs.current[flyRequest.id];
      if (marker) {
        marker.openPopup();
      }
    }, 450);
    return () => clearTimeout(timer);
  }, [flyRequest, map, markerRefs]);
  return null;
}

export default function App() {
  const [dispatches, setDispatches] = useState<Dispatch[]>([]);
  const [stations, setStations] = useState<Station[]>([]);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [flyRequest, setFlyRequest] = useState<{
    id: number;
    lat: number;
    lon: number;
    seq: number;
  } | null>(null);
  const [mobileView, setMobileView] = useState<'split' | 'map' | 'list'>('split');

  // Filters
  const [hours, setHours] = useState<number | null>(72); // default 3 days
  const [agencyFilter, setAgencyFilter] = useState<string>('all');
  const [categoryFilter, setCategoryFilter] = useState<string>('all');
  const [precisionFilter, setPrecisionFilter] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [limit, setLimit] = useState<number>(300);

  // UI controls
  const [tileStyle, setTileStyle] = useState<'dark' | 'osm' | 'light'>('dark');
  const [showStations, setShowStations] = useState<boolean>(false);
  const [enableClustering, setEnableClustering] = useState<boolean>(true);
  const [liveRefresh, setLiveRefresh] = useState<boolean>(true);
  const [loading, setLoading] = useState<boolean>(false);
  const [reparsingId, setReparsingId] = useState<number | null>(null);
  const [batchUpgrading, setBatchUpgrading] = useState<boolean>(false);

  // Edit location modal state
  const [editingDispatch, setEditingDispatch] = useState<Dispatch | null>(null);
  const [editAddress, setEditAddress] = useState('');
  const [editMuni, setEditMuni] = useState('');
  const [editCross, setEditCross] = useState('');
  const [editCategory, setEditCategory] = useState('');

  const markerRefs = useRef<Record<number, L.Marker | null>>({});
  const cardRefs = useRef<Record<number, HTMLDivElement | null>>({});

  const fetchAll = async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const params = new URLSearchParams();
      if (hours !== null) params.set('hours', String(hours));
      if (agencyFilter !== 'all') params.set('agency', agencyFilter);
      if (categoryFilter !== 'all') params.set('incident_type', categoryFilter);
      if (precisionFilter !== 'all') params.set('precision', precisionFilter);
      if (searchQuery.trim()) params.set('search', searchQuery.trim());
      params.set('limit', String(limit));

      const statsParams = new URLSearchParams();
      if (hours !== null) statsParams.set('hours', String(hours));

      const [dispRes, statsRes] = await Promise.all([
        fetch(`/api/dispatches?${params.toString()}`),
        fetch(`/api/stats?${statsParams.toString()}`),
      ]);
      if (dispRes.ok) {
        const dData = await dispRes.json();
        setDispatches(dData.dispatches || []);
      }
      if (statsRes.ok) {
        const sData = await statsRes.json();
        setStats(sData);
      }
    } catch (err) {
      console.error('Failed to fetch dispatches:', err);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    fetch('/api/stations')
      .then((r) => r.json())
      .then((d) => setStations(d.stations || []))
      .catch(console.error);
  }, []);

  useEffect(() => {
    const t = setTimeout(() => fetchAll(false), 120);
    return () => clearTimeout(t);
  }, [hours, agencyFilter, categoryFilter, precisionFilter, searchQuery, limit]);

  useEffect(() => {
    if (!liveRefresh) return;
    const interval = setInterval(() => {
      fetchAll(true);
    }, 10000);
    return () => clearInterval(interval);
  }, [liveRefresh, hours, agencyFilter, categoryFilter, precisionFilter, searchQuery, limit]);

  const selectedDispatch = useMemo(
    () => dispatches.find((d) => d.id === selectedId) || null,
    [dispatches, selectedId]
  );

  const handleSelectFromMap = (d: Dispatch) => {
    setSelectedId(d.id);
    const el = cardRefs.current[d.id];
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  };

  const handleReparse = async (id: number, e?: React.MouseEvent) => {
    if (e) e.stopPropagation();
    setReparsingId(id);
    try {
      const res = await fetch(`/api/dispatches/${id}/reparse?mode=llm`, {
        method: 'POST',
      });
      if (res.ok) {
        const { dispatch } = await res.json();
        setDispatches((prev) => prev.map((item) => (item.id === id ? dispatch : item)));
        setSelectedId(id);
      }
    } catch (err) {
      console.error('LLM reparse failed:', err);
    } finally {
      setReparsingId(null);
    }
  };

  const handleBatchUpgrade = async () => {
    setBatchUpgrading(true);
    try {
      await fetch('/api/backfill/batch?limit=15', { method: 'POST' });
      setTimeout(() => {
        fetchAll(true);
        setBatchUpgrading(false);
      }, 4500);
    } catch (err) {
      console.error(err);
      setBatchUpgrading(false);
    }
  };

  const openEditModal = (d: Dispatch, e?: React.MouseEvent) => {
    if (e) e.stopPropagation();
    setEditingDispatch(d);
    setEditAddress(d.extracted_address || '');
    setEditMuni(d.municipality || '');
    setEditCross(d.cross_streets || '');
    setEditCategory(d.incident_type || 'Other');
  };

  const saveEditLocation = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingDispatch) return;
    try {
      const res = await fetch(`/api/dispatches/${editingDispatch.id}/location`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          extracted_address: editAddress.trim() || null,
          municipality: editMuni.trim() || null,
          cross_streets: editCross.trim() || null,
          incident_type: editCategory,
          regeocode: true,
        }),
      });
      if (res.ok) {
        const { dispatch } = await res.json();
        setDispatches((prev) =>
          prev.map((item) => (item.id === editingDispatch.id ? dispatch : item))
        );
        setEditingDispatch(null);
      }
    } catch (err) {
      console.error('Failed to save location edit:', err);
    }
  };

  const renderPopupCard = (d: Dispatch) => {
    const cfg = CATEGORY_CONFIG[d.incident_type] || CATEGORY_CONFIG.Other;
    const { relative, localFormatted } = formatTimestamp(d.timestamp);
    const isLlm = d.parse_method === 'llm';
    const isExact = d.location_precision === 'exact';
    const isApprox = d.location_precision === 'approximate';

    return (
      <div className="p-3.5 text-slate-100 text-xs space-y-2.5">
        {/* Header badges */}
        <div className="flex items-center justify-between gap-2 pr-4">
          <span
            className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md font-semibold border ${cfg.bg} ${cfg.border} ${cfg.text}`}
          >
            <span
              className="w-2 h-2 rounded-full"
              style={{ backgroundColor: cfg.color }}
            />
            {d.incident_type}
          </span>
          <span className="text-slate-400 font-mono text-[11px]">#{d.id}</span>
        </div>

        {/* Agency & Time */}
        <div>
          <div className="font-bold text-sm text-white leading-snug">{d.agency}</div>
          <div className="flex items-center gap-1.5 text-slate-400 text-[11px] mt-0.5">
            <Clock className="w-3 h-3 text-slate-500" />
            <span>{localFormatted}</span>
            <span className="text-orange-400 font-medium">({relative})</span>
            {d.total_duration_sec ? (
              <span className="text-slate-500">• {Math.round(d.total_duration_sec)}s</span>
            ) : null}
          </div>
        </div>

        {/* Address & Cross Streets */}
        <div className="bg-slate-900/90 border border-slate-800 rounded-lg p-2.5 space-y-1">
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-start gap-1.5">
              <MapPin className="w-3.5 h-3.5 text-orange-400 shrink-0 mt-0.5" />
              <div>
                <div className="font-semibold text-slate-100">
                  {d.extracted_address ? (
                    <>
                      {d.extracted_address}
                      {d.municipality ? `, ${d.municipality}` : ''}
                    </>
                  ) : (
                    <span className="text-slate-400 italic">
                      {d.municipality || 'Station Area'} (Station Fallback)
                    </span>
                  )}
                </div>
                {d.cross_streets && (
                  <div className="text-[11px] text-slate-400">
                    Cross: <span className="text-slate-300">{d.cross_streets}</span>
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className="flex items-center justify-between pt-1 border-t border-slate-800/80 text-[10px]">
            <span
              className={`px-1.5 py-0.5 rounded font-medium ${
                isExact
                  ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                  : isApprox
                  ? 'bg-amber-500/15 text-amber-300 border border-amber-500/30'
                  : 'bg-slate-800 text-slate-400 border border-slate-700'
              }`}
            >
              {isExact
                ? 'Exact OSM Pin'
                : isApprox
                ? 'Approx Street Match'
                : 'Dept Station Area'}
            </span>

            <span
              className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-medium ${
                isLlm
                  ? 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/40'
                  : d.parse_method === 'manual'
                  ? 'bg-teal-500/20 text-teal-300 border border-teal-500/40'
                  : 'bg-slate-800 text-slate-400'
              }`}
            >
              {isLlm ? '✨ LLM Parsed' : d.parse_method === 'manual' ? '✎ Manual' : 'Regex'}
            </span>
          </div>
        </div>

        {/* Full Transcript */}
        <div className="bg-slate-950/70 border border-slate-800/80 rounded-lg p-2.5 text-slate-200 leading-relaxed max-h-32 overflow-y-auto text-[12px]">
          "{d.message}"
        </div>

        {/* Audio Player */}
        {d.audio_stream_url && (
          <div className="pt-0.5">
            <div className="flex items-center gap-1.5 text-[11px] text-slate-400 mb-1">
              <Volume2 className="w-3.5 h-3.5 text-orange-400" />
              <span>Dispatch Audio Recording</span>
            </div>
            <audio
              controls
              preload="none"
              className="w-full h-8 rounded"
              src={d.audio_stream_url}
            />
          </div>
        )}

        {/* Tones & Actions Footer */}
        <div className="flex items-center justify-between pt-1 border-t border-slate-800/80 gap-2">
          <div className="text-[10px] text-slate-500 font-mono truncate">
            {d.tones && d.tones.length > 0
              ? `Tones: ${d.tones.map((t) => `${t}Hz`).join(' / ')}`
              : d.frequency}
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <button
              onClick={(e) => handleReparse(d.id, e)}
              disabled={reparsingId === d.id}
              title="Re-parse transcript with Gemma LLM and re-geocode"
              className="inline-flex items-center gap-1 px-2 py-1 rounded bg-indigo-600/80 hover:bg-indigo-500 text-white text-[11px] font-medium transition disabled:opacity-50"
            >
              <Sparkles className="w-3 h-3" />
              {reparsingId === d.id ? 'Parsing...' : 'LLM Parse'}
            </button>
            <button
              onClick={(e) => openEditModal(d, e)}
              title="Manually edit address or incident type"
              className="inline-flex items-center gap-1 px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-[11px] font-medium transition border border-slate-700"
            >
              <Edit3 className="w-3 h-3" />
              Edit
            </button>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="flex flex-col h-[100dvh] w-screen bg-slate-950 text-slate-100 overflow-hidden">
      {/* Top Command Bar */}
      <header className="border-b border-slate-800/90 bg-slate-900/95 backdrop-blur px-3 py-2 md:py-0 md:h-14 flex flex-wrap items-center justify-between gap-2 shrink-0 z-20">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 md:w-9 md:h-9 rounded-lg bg-gradient-to-br from-orange-500 to-red-600 flex items-center justify-center shadow-lg shadow-orange-600/20 shrink-0">
            <Flame className="w-4 h-4 md:w-5 md:h-5 text-white" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="font-bold text-sm md:text-base tracking-tight text-white">
                FIRECALL <span className="text-orange-400 font-semibold">CAD MAP</span>
              </h1>
              <span className="hidden sm:inline-block text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                Orange County, NY • 154.205 MHz
              </span>
            </div>
            <p className="text-[11px] text-slate-400 hidden lg:block">
              Live Quick-Call II Paging, Whisper STT &amp; Hybrid LLM/OpenStreetMap Geocoder
            </p>
          </div>
        </div>

        {/* Mobile View Switcher (Map | Split | Calls) - visible on screens < md */}
        <div className="flex md:hidden items-center bg-slate-950 p-0.5 rounded-lg border border-slate-800 text-[11px] font-medium">
          {[
            { id: 'map', label: 'Map' },
            { id: 'split', label: 'Split' },
            { id: 'list', label: `Calls (${dispatches.length})` },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setMobileView(tab.id as 'split' | 'map' | 'list')}
              className={`px-2.5 py-1 rounded-md transition ${
                mobileView === tab.id
                  ? 'bg-orange-500 text-white font-semibold'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Time Filter Pills */}
        <div className="flex items-center gap-1 bg-slate-950/90 p-1 rounded-lg border border-slate-800 overflow-x-auto max-w-full">
          {[
            { label: '24h', val: 24 },
            { label: '3d', val: 72 },
            { label: '7d', val: 168 },
            { label: '30d', val: 720 },
            { label: 'All', val: null },
          ].map((t) => (
            <button
              key={t.label}
              onClick={() => setHours(t.val)}
              className={`px-2 md:px-2.5 py-1 rounded-md text-[11px] md:text-xs font-medium transition whitespace-nowrap ${
                hours === t.val
                  ? 'bg-orange-500 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Right Controls & Status */}
        <div className="flex items-center gap-1.5 md:gap-2">
          {stats?.llm?.enabled && (
            <button
              onClick={handleBatchUpgrade}
              disabled={batchUpgrading}
              title={`Run ${stats.llm.model} + Nominatim on 15 more recent dispatches in background`}
              className="inline-flex items-center gap-1 px-2 md:px-2.5 py-1 md:py-1.5 rounded-lg bg-indigo-500/15 hover:bg-indigo-500/25 border border-indigo-500/30 text-indigo-300 text-[11px] md:text-xs font-medium transition disabled:opacity-50"
            >
              <Sparkles className={`w-3.5 h-3.5 ${batchUpgrading ? 'animate-spin' : ''}`} />
              <span className="hidden sm:inline">
                {batchUpgrading
                  ? 'LLM Geocoding...'
                  : `LLM Upgrade (${stats.by_parse_method?.llm || 0} parsed)`}
              </span>
              <span className="sm:hidden">{stats.by_parse_method?.llm || 0}</span>
            </button>
          )}

          <button
            onClick={() => setLiveRefresh((v) => !v)}
            className={`inline-flex items-center gap-1.5 px-2 md:px-2.5 py-1 md:py-1.5 rounded-lg text-[11px] md:text-xs font-medium border transition ${
              liveRefresh
                ? 'bg-emerald-500/15 border-emerald-500/30 text-emerald-400'
                : 'bg-slate-800 border-slate-700 text-slate-400'
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                liveRefresh ? 'bg-emerald-400 animate-ping' : 'bg-slate-500'
              }`}
            />
            <span className="hidden sm:inline">{liveRefresh ? 'Live' : 'Paused'}</span>
          </button>

          <button
            onClick={() => fetchAll(false)}
            className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition"
            title="Refresh dispatches now"
          >
            <RefreshCw className={`w-3.5 h-3.5 md:w-4 md:h-4 ${loading ? 'animate-spin text-orange-400' : ''}`} />
          </button>
        </div>
      </header>

      {/* Secondary Category & Filter Strip */}
      <div className="bg-slate-900/60 border-b border-slate-800/80 px-3 py-1.5 flex items-center justify-between gap-2 shrink-0 z-10 overflow-x-auto">
        {/* Category Pills */}
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            onClick={() => setCategoryFilter('all')}
            className={`px-2.5 py-1 rounded-full text-[11px] md:text-xs font-medium border transition whitespace-nowrap ${
              categoryFilter === 'all'
                ? 'bg-slate-100 text-slate-900 border-white font-semibold'
                : 'bg-slate-900 text-slate-400 border-slate-800 hover:border-slate-700'
            }`}
          >
            All ({dispatches.length})
          </button>
          {Object.entries(CATEGORY_CONFIG).map(([cat, cfg]) => {
            const count = stats?.by_category?.[cat] || 0;
            const active = categoryFilter === cat;
            return (
              <button
                key={cat}
                onClick={() => setCategoryFilter(active ? 'all' : cat)}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] md:text-xs font-medium border transition whitespace-nowrap ${
                  active
                    ? `${cfg.bg} ${cfg.border} ${cfg.text} ring-1 ring-current`
                    : 'bg-slate-900/90 text-slate-400 border-slate-800 hover:border-slate-700 hover:text-slate-200'
                }`}
              >
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: cfg.color }}
                />
                <span>{cfg.shortLabel}</span>
                <span className="text-[10px] opacity-75 font-mono">({count})</span>
              </button>
            );
          })}
        </div>

        {/* Map Layer & Precision Controls */}
        <div className="flex items-center gap-1.5 text-xs shrink-0">
          <select
            value={precisionFilter}
            onChange={(e) => setPrecisionFilter(e.target.value)}
            className="bg-slate-900 border border-slate-800 rounded-lg px-2 py-1 text-[11px] md:text-xs text-slate-300 focus:outline-none focus:border-orange-500"
          >
            <option value="all">All Pins</option>
            <option value="exact">Exact ({stats?.by_precision?.exact || 0})</option>
            <option value="approximate">
              Approx ({stats?.by_precision?.approximate || 0})
            </option>
            <option value="station_fallback">
              Station ({stats?.by_precision?.station_fallback || 0})
            </option>
          </select>

          <button
            onClick={() => setShowStations((s) => !s)}
            className={`inline-flex items-center gap-1 px-2 py-1 rounded-lg border text-[11px] md:text-xs transition whitespace-nowrap ${
              showStations
                ? 'bg-orange-500/20 border-orange-500/40 text-orange-300'
                : 'bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200'
            }`}
          >
            <Building2 className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">FD Stations ({stations.length})</span>
            <span className="sm:hidden">FDs</span>
          </button>

          <button
            onClick={() => setEnableClustering((c) => !c)}
            className={`inline-flex items-center gap-1 px-2 py-1 rounded-lg border text-[11px] md:text-xs transition whitespace-nowrap ${
              enableClustering
                ? 'bg-blue-500/20 border-blue-500/40 text-blue-300'
                : 'bg-slate-900 border-slate-800 text-slate-400 hover:text-slate-200'
            }`}
          >
            <Layers className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">Cluster</span>
          </button>

          <div className="flex items-center bg-slate-900 border border-slate-800 rounded-lg p-0.5">
            {(['dark', 'osm', 'light'] as const).map((styleKey) => (
              <button
                key={styleKey}
                onClick={() => setTileStyle(styleKey)}
                className={`px-2 py-0.5 rounded text-[11px] font-medium transition whitespace-nowrap ${
                  tileStyle === styleKey
                    ? 'bg-slate-700 text-white'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {styleKey === 'dark' ? 'Dark' : styleKey === 'osm' ? 'OSM' : 'Light'}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Main Content Split: Responsive Mobile (Map on top, List below or Tabbed) + Desktop Side-by-Side */}
      <div className="flex-1 flex flex-col md:flex-row overflow-hidden relative">
        {/* Synchronized Sidebar / Bottom Call Drawer on Mobile */}
        <aside
          className={`w-full md:w-96 border-t md:border-t-0 md:border-r border-slate-800/90 bg-slate-900/85 flex flex-col shrink-0 z-10 order-2 md:order-1 ${
            mobileView === 'map'
              ? 'hidden md:flex'
              : mobileView === 'list'
              ? 'flex-1 h-full'
              : 'h-[44%] md:h-full'
          }`}
        >
          {/* Search & Agency Filter inside Sidebar */}
          <div className="p-2.5 md:p-3 border-b border-slate-800/90 space-y-2 bg-slate-900/95">
            <div className="relative">
              <Search className="w-4 h-4 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search street, town, transcript, keyword..."
                className="w-full bg-slate-950 border border-slate-800 rounded-lg pl-9 pr-8 py-1.5 text-xs text-slate-100 placeholder-slate-500 focus:outline-none focus:border-orange-500"
              />
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery('')}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>

            <div className="flex items-center gap-2">
              <select
                value={agencyFilter}
                onChange={(e) => setAgencyFilter(e.target.value)}
                className="flex-1 min-w-0 bg-slate-950 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-orange-500"
              >
                <option value="all">All Orange County Agencies</option>
                {stats?.top_agencies?.map((a) => (
                  <option key={a.agency} value={a.agency}>
                    {a.agency} ({a.count})
                  </option>
                ))}
              </select>

              <select
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
                className="bg-slate-950 border border-slate-800 rounded-lg px-2 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-orange-500"
                title="Max pins to render"
              >
                <option value={100}>100 pins</option>
                <option value={300}>300 pins</option>
                <option value={750}>750 pins</option>
                <option value={2500}>All ({stats?.total_all_time || 2200})</option>
              </select>
            </div>
          </div>

          {/* Dispatch Cards List */}
          <div className="flex-1 overflow-y-auto divide-y divide-slate-800/70">
            {dispatches.length === 0 ? (
              <div className="p-8 text-center text-slate-500 text-xs space-y-2">
                <Radio className="w-8 h-8 mx-auto text-slate-600 stroke-1" />
                <div>No dispatches match your current filters.</div>
              </div>
            ) : (
              dispatches.map((d) => {
                const cfg = CATEGORY_CONFIG[d.incident_type] || CATEGORY_CONFIG.Other;
                const { relative, localFormatted, isRecent } = formatTimestamp(d.timestamp);
                const isSelected = d.id === selectedId;
                const isExact = d.location_precision === 'exact';
                const isApprox = d.location_precision === 'approximate';

                return (
                  <div
                    key={d.id}
                    ref={(el) => (cardRefs.current[d.id] = el)}
                    onClick={() => {
                      setSelectedId(d.id);
                      if (mobileView === 'list') {
                        setMobileView('split');
                      }
                      setFlyRequest((prev) => ({
                        id: d.id,
                        lat: d.latitude,
                        lon: d.longitude,
                        seq: (prev?.seq || 0) + 1,
                      }));
                    }}
                    className={`p-2.5 md:p-3 cursor-pointer transition relative ${
                      isSelected
                        ? 'bg-slate-800/95 border-l-4 border-l-orange-500'
                        : 'hover:bg-slate-800/40'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <span
                        className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-semibold border ${cfg.bg} ${cfg.border} ${cfg.text}`}
                      >
                        <span
                          className="w-1.5 h-1.5 rounded-full"
                          style={{ backgroundColor: cfg.color }}
                        />
                        {d.incident_type}
                      </span>

                      <div className="flex items-center gap-1.5 text-[11px] text-slate-400">
                        {isRecent && (
                          <span className="px-1.5 py-0.2 rounded bg-red-500/20 text-red-400 text-[10px] font-semibold">
                            NEW
                          </span>
                        )}
                        <span title={localFormatted}>{relative}</span>
                        <span className="text-slate-600 font-mono">#{d.id}</span>
                      </div>
                    </div>

                    <div className="font-bold text-xs text-white truncate">{d.agency}</div>

                    {/* Extracted Location Row */}
                    <div className="flex items-center justify-between gap-2 mt-1">
                      <div className="flex items-center gap-1 text-xs text-orange-300/95 font-medium truncate">
                        <MapPin className="w-3.5 h-3.5 shrink-0 text-orange-400" />
                        <span className="truncate">
                          {d.extracted_address
                            ? `${d.extracted_address}${d.municipality ? `, ${d.municipality}` : ''}`
                            : `${d.municipality || 'Station Area'} (Dept Station)`}
                        </span>
                      </div>

                      <div className="flex items-center gap-1 shrink-0">
                        {d.parse_method === 'llm' && (
                          <span
                            className="text-[10px] px-1.5 py-0.2 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30"
                            title="Parsed by Gemma LLM"
                          >
                            LLM
                          </span>
                        )}
                        <span
                          className={`w-2 h-2 rounded-full ${
                            isExact
                              ? 'bg-emerald-400'
                              : isApprox
                              ? 'bg-amber-400'
                              : 'bg-slate-500'
                          }`}
                          title={
                            isExact
                              ? 'Exact OpenStreetMap Coordinate'
                              : isApprox
                              ? 'Approximate Road Coordinate'
                              : 'Fire Station Coordinate Fallback'
                          }
                        />
                      </div>
                    </div>

                    <p className="text-[11px] text-slate-400 line-clamp-2 mt-1 leading-relaxed">
                      "{d.message}"
                    </p>

                    {isSelected && d.audio_stream_url && (
                      <div className="mt-2 pt-2 border-t border-slate-700/70" onClick={(e) => e.stopPropagation()}>
                        <audio
                          controls
                          preload="none"
                          className="w-full h-7"
                          src={d.audio_stream_url}
                        />
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>

          {/* Sidebar Footer Stats */}
          <div className="p-2 md:p-2.5 border-t border-slate-800 bg-slate-950 text-[11px] text-slate-400 flex items-center justify-between">
            <span>
              Showing <strong className="text-white">{dispatches.length}</strong> of{' '}
              <strong className="text-white">{stats?.total_all_time || 0}</strong> calls
            </span>
            <span>
              24h: <strong className="text-orange-400">{stats?.last_24h || 0}</strong> • 7d:{' '}
              <strong className="text-orange-400">{stats?.last_7d || 0}</strong>
            </span>
          </div>
        </aside>

        {/* Right OpenStreetMap Canvas (Top on Mobile, Right on Desktop) */}
        <main
          className={`flex-1 relative order-1 md:order-2 ${
            mobileView === 'list' ? 'hidden md:block' : 'block'
          }`}
        >
          <MapContainer
            center={[41.41, -74.32]}
            zoom={10}
            className="w-full h-full z-0"
            preferCanvas={true}
          >
            <TileLayer
              key={`${tileStyle}-${stats?.map_config?.carto_api_key || ''}`}
              url={
                stats?.map_config?.carto_api_key && tileStyle !== 'osm'
                  ? `${MAP_TILES[tileStyle].url}?key=${encodeURIComponent(
                      stats.map_config.carto_api_key
                    )}`
                  : MAP_TILES[tileStyle].url
              }
              attribution={MAP_TILES[tileStyle].attribution}
            />

            <MapFlyToController
              flyRequest={flyRequest}
              markerRefs={markerRefs}
              mobileView={mobileView}
            />

            {/* Optional Fire Department Stations Layer */}
            {showStations &&
              stations.map((st) => (
                <Marker key={st.id} position={[st.lat, st.lon]} icon={stationIcon}>
                  <Tooltip direction="top" offset={[0, -10]} opacity={0.95}>
                    <div className="text-xs font-semibold">{st.name}</div>
                    <div className="text-[10px] text-slate-500">{st.address}</div>
                  </Tooltip>
                  <Popup>
                    <div className="p-3 text-xs space-y-1 text-slate-100">
                      <div className="text-[10px] uppercase tracking-wider text-orange-400 font-bold">
                        Fire Department Station
                      </div>
                      <div className="font-bold text-sm">{st.name}</div>
                      <div className="text-slate-300">{st.address}</div>
                      <div className="text-slate-400 text-[11px]">
                        Municipality: {st.municipality} (Town of {st.town})
                      </div>
                      <button
                        onClick={() => setAgencyFilter(st.id)}
                        className="mt-2 w-full py-1 rounded bg-orange-500 hover:bg-orange-600 text-white font-medium text-xs transition"
                      >
                        Filter Calls for {st.id}
                      </button>
                    </div>
                  </Popup>
                </Marker>
              ))}

            {/* Dispatch Pins (Clustered or Unclustered) */}
            {enableClustering ? (
              <MarkerClusterGroup
                chunkedLoading
                maxClusterRadius={42}
                spiderfyOnMaxZoom={true}
                showCoverageOnHover={false}
              >
                {dispatches.map((d) => {
                  const { isRecent } = formatTimestamp(d.timestamp);
                  const isSelected = d.id === selectedId;
                  return (
                    <Marker
                      key={d.id}
                      position={[d.latitude, d.longitude]}
                      icon={createIncidentIcon(
                        d.incident_type,
                        isSelected,
                        isRecent,
                        d.location_precision
                      )}
                      ref={(r) => {
                        markerRefs.current[d.id] = r;
                      }}
                      eventHandlers={{
                        click: () => handleSelectFromMap(d),
                        popupclose: () => {
                          setSelectedId((prev) => (prev === d.id ? null : prev));
                        },
                      }}
                    >
                      <Popup>{renderPopupCard(d)}</Popup>
                    </Marker>
                  );
                })}
              </MarkerClusterGroup>
            ) : (
              dispatches.map((d) => {
                const { isRecent } = formatTimestamp(d.timestamp);
                const isSelected = d.id === selectedId;
                return (
                  <Marker
                    key={d.id}
                    position={[d.latitude, d.longitude]}
                    icon={createIncidentIcon(
                      d.incident_type,
                      isSelected,
                      isRecent,
                      d.location_precision
                    )}
                    ref={(r) => {
                      markerRefs.current[d.id] = r;
                    }}
                    eventHandlers={{
                      click: () => handleSelectFromMap(d),
                      popupclose: () => {
                        setSelectedId((prev) => (prev === d.id ? null : prev));
                      },
                    }}
                  >
                    <Popup>{renderPopupCard(d)}</Popup>
                  </Marker>
                );
              })
            )}
          </MapContainer>

          {/* Floating Map Legend Overlay (Hidden on small phones to save map space, shown on sm+) */}
          <div className="hidden sm:block absolute bottom-5 right-4 z-[400] bg-slate-900/90 backdrop-blur-md border border-slate-800 rounded-xl p-3 shadow-xl text-xs space-y-2 max-w-xs pointer-events-auto">
            <div className="font-semibold text-slate-200 flex items-center justify-between">
              <span>Pin Legend</span>
              <span className="text-[10px] text-slate-400 font-normal">Click pin for audio</span>
            </div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
              {Object.entries(CATEGORY_CONFIG).map(([cat, cfg]) => (
                <div key={cat} className="flex items-center gap-1.5 text-slate-300">
                  <span
                    className="w-2.5 h-2.5 rounded-full shrink-0 border border-white/80"
                    style={{ backgroundColor: cfg.color }}
                  />
                  <span className="truncate">{cfg.shortLabel}</span>
                </div>
              ))}
            </div>
            <div className="pt-1.5 border-t border-slate-800 flex items-center justify-between text-[10px] text-slate-400 gap-3">
              <span className="flex items-center gap-1">
                <span className="w-2.5 h-2.5 rounded-full border-2 border-white inline-block bg-slate-600" />
                Solid = Street Match
              </span>
              <span className="flex items-center gap-1">
                <span className="w-2.5 h-2.5 rounded-full border-2 border-dashed border-white inline-block bg-slate-600" />
                Dashed = Station Area
              </span>
            </div>
          </div>
        </main>
      </div>

      {/* Edit Location Modal */}
      {editingDispatch && (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-700 rounded-xl max-w-md w-full p-5 shadow-2xl space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="font-bold text-sm text-white">
                Edit Location &amp; Incident — Dispatch #{editingDispatch.id}
              </h3>
              <button
                onClick={() => setEditingDispatch(null)}
                className="text-slate-400 hover:text-white"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-2.5 rounded-lg bg-slate-950 border border-slate-800 text-xs text-slate-300 leading-relaxed">
              "{editingDispatch.message}"
            </div>

            <form onSubmit={saveEditLocation} className="space-y-3 text-xs">
              <div>
                <label className="block text-slate-400 mb-1">Incident Category</label>
                <select
                  value={editCategory}
                  onChange={(e) => setEditCategory(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-slate-100"
                >
                  {Object.keys(CATEGORY_CONFIG).map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-slate-400 mb-1">
                  Street Address or Intersection (Re-geocodes via OpenStreetMap)
                </label>
                <input
                  type="text"
                  value={editAddress}
                  onChange={(e) => setEditAddress(e.target.value)}
                  placeholder="e.g. 9 Peach Terrace or County Route 12 & Gardnerville Rd"
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-slate-100"
                />
              </div>

              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="block text-slate-400 mb-1">Municipality / Town</label>
                  <input
                    type="text"
                    value={editMuni}
                    onChange={(e) => setEditMuni(e.target.value)}
                    placeholder="e.g. Newburgh"
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-slate-100"
                  />
                </div>
                <div>
                  <label className="block text-slate-400 mb-1">Cross Streets</label>
                  <input
                    type="text"
                    value={editCross}
                    onChange={(e) => setEditCross(e.target.value)}
                    placeholder="e.g. Brooker Drive"
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-slate-100"
                  />
                </div>
              </div>

              <div className="flex items-center justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setEditingDispatch(null)}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-1.5 rounded-lg bg-orange-500 hover:bg-orange-600 text-white font-semibold"
                >
                  Save &amp; Geocode
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
