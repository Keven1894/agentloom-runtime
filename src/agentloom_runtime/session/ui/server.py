"""Layer 0 Session Web Viewer — standalone HTTP server and REST API.

Provides a zero-configuration web interface for humans to browse conversation
transcripts, explore session DAG lineage, and search historical decisions.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import urllib.parse
import webbrowser
from typing import Any, Optional

from agentloom_runtime.session import store
from agentloom_runtime.session.identity import detect_workspace_key

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="h-full bg-slate-900 text-slate-100">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AgentLoom — Layer 0 Session Memory Viewer</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    [v-cloak] { display: none; }
    pre code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: rgba(15, 23, 42, 0.6); }
    ::-webkit-scrollbar-thumb { background: rgba(51, 65, 85, 0.8); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(71, 85, 105, 1); }
  </style>
</head>
<body class="h-full flex flex-col overflow-hidden">
  <div id="app" v-cloak class="flex flex-col h-full">
    <!-- Top Navbar -->
    <header class="bg-slate-800/90 backdrop-blur border-b border-slate-700/60 px-4 py-2.5 flex items-center justify-between z-10 shrink-0">
      <div class="flex items-center space-x-3">
        <div class="w-8 h-8 rounded-lg bg-gradient-to-tr from-indigo-500 to-cyan-400 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/20">
          <i class="fa-solid fa-network-wired text-sm"></i>
        </div>
        <div>
          <h1 class="text-sm font-semibold tracking-wide text-slate-100 flex items-center gap-2">
            AgentLoom <span class="text-xs px-2 py-0.5 rounded bg-indigo-950 text-indigo-400 border border-indigo-800/60 font-mono">Layer 0 Session OS</span>
          </h1>
        </div>
      </div>

      <!-- Workspace selector & search -->
      <div class="flex items-center space-x-3 flex-1 max-w-2xl mx-6">
        <div class="relative w-1/3">
          <select v-model="selectedWorkspace" @change="onWorkspaceChange" class="w-full bg-slate-900/90 text-xs border border-slate-700 rounded-lg px-3 py-1.5 text-slate-200 focus:outline-none focus:border-indigo-500 font-mono truncate">
            <option v-for="ws in workspaces" :key="ws" :value="ws">{{ ws }}</option>
          </select>
        </div>
        <div class="relative flex-1">
          <i class="fa-solid fa-magnifying-glass absolute left-3 top-2.5 text-xs text-slate-400"></i>
          <input type="text" v-model="searchQuery" @keyup.enter="performSearch" placeholder="Search archive (decisions, topics, plans)..." class="w-full bg-slate-900/90 text-xs border border-slate-700 rounded-lg pl-8 pr-8 py-1.5 text-slate-200 focus:outline-none focus:border-indigo-500 placeholder-slate-500">
          <button v-if="searchQuery" @click="clearSearch" class="absolute right-2.5 top-2 text-xs text-slate-400 hover:text-slate-200">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </div>
      </div>

      <div class="flex items-center space-x-2 text-xs">
        <button v-if="selectedSession" @click="showSessionPanel = !showSessionPanel" class="px-2.5 py-1.5 rounded-lg border text-slate-300 transition flex items-center gap-1.5" :class="showSessionPanel ? 'bg-indigo-950/80 border-indigo-800/80 text-indigo-300' : 'bg-slate-800/60 border-slate-700/60 hover:bg-slate-700'">
          <i class="fa-solid fa-cube"></i> Details
        </button>
        <button @click="refreshAll" :disabled="loading" class="px-2.5 py-1.5 rounded-lg bg-slate-700/60 hover:bg-slate-700 text-slate-300 transition flex items-center gap-1.5">
          <i class="fa-solid fa-arrows-rotate" :class="{'fa-spin': loading}"></i> Refresh
        </button>
      </div>
    </header>

    <!-- Main Workspace Area (3-Column Layout) -->
    <div class="flex-1 flex overflow-hidden">
      <!-- Left Column: Session DAG Tree & Transcripts -->
      <aside class="w-80 min-w-[280px] max-w-[340px] bg-slate-900/80 border-r border-slate-800 flex flex-col shrink-0">
        <!-- Tab selector -->
        <div class="flex border-b border-slate-800 text-xs font-medium shrink-0">
          <button @click="leftTab = 'tree'" :class="{'text-indigo-400 border-b-2 border-indigo-500 bg-slate-800/40': leftTab === 'tree', 'text-slate-400 hover:text-slate-200': leftTab !== 'tree'}" class="flex-1 py-2.5 text-center transition flex items-center justify-center gap-1.5">
            <i class="fa-solid fa-sitemap"></i> Session DAG
          </button>
          <button @click="leftTab = 'transcripts'" :class="{'text-indigo-400 border-b-2 border-indigo-500 bg-slate-800/40': leftTab === 'transcripts', 'text-slate-400 hover:text-slate-200': leftTab !== 'transcripts'}" class="flex-1 py-2.5 text-center transition flex items-center justify-center gap-1.5">
            <i class="fa-solid fa-comments"></i> Transcripts ({{ totalTranscripts }})
          </button>
        </div>

        <!-- Search Results View (Overrides tab if active search) -->
        <div v-if="searchResults !== null" class="flex-1 overflow-y-auto p-2 space-y-2">
          <div class="flex items-center justify-between px-2 py-1 text-xs text-slate-400">
            <span class="font-semibold">Search Results ({{ searchResults.length }})</span>
            <button @click="clearSearch" class="text-indigo-400 hover:underline">Clear</button>
          </div>
          <div v-if="searchResults.length === 0" class="p-6 text-center text-xs text-slate-500">
            No matching conversation snippets found.
          </div>
          <div v-for="hit in searchResults" :key="hit.chunk_id" @click="selectSearchHit(hit)" class="p-2.5 rounded-lg border border-slate-800 hover:border-indigo-500/50 bg-slate-800/30 hover:bg-slate-800/60 cursor-pointer transition text-xs space-y-1.5">
            <div class="flex items-center justify-between text-slate-400 font-mono text-[10px]">
              <span class="truncate max-w-[140px]" :title="hit.source_ref">Ref: {{ hit.source_ref.slice(0, 8) }}</span>
              <span class="text-emerald-400 font-semibold">Score: {{ hit.score.toFixed(2) }}</span>
            </div>
            <div class="text-slate-300 line-clamp-3 text-[11px] leading-relaxed">
              {{ hit.snippet }}
            </div>
            <div class="text-[10px] text-slate-500 flex items-center justify-between font-mono">
              <span>Turns: {{ hit.seq_start }}–{{ hit.seq_end }}</span>
              <span>{{ hit.captured_at ? hit.captured_at.slice(0, 10) : '' }}</span>
            </div>
          </div>
        </div>

        <!-- Tree View Tab -->
        <div v-else-if="leftTab === 'tree'" class="flex-1 overflow-y-auto p-2 space-y-1">
          <div v-if="sessionTree.length === 0" class="p-6 text-center text-xs text-slate-500">
            No session DAG nodes recorded for this workspace.
          </div>
          <div v-for="node in sessionTree" :key="node.session_id">
            <tree-node :node="node" :selected-id="selectedSessionId" @select="selectSession"></tree-node>
          </div>
        </div>

        <!-- Transcripts Tab -->
        <div v-else class="flex-1 flex flex-col overflow-hidden">
          <!-- Transcripts Count & Page Size Bar -->
          <div class="px-3 py-2 border-b border-slate-800/80 bg-slate-900/40 flex items-center justify-between text-[11px] text-slate-400 shrink-0">
            <span class="font-medium text-slate-300">
              <span v-if="totalTranscripts > 0">{{ transcriptPageStart }}–{{ transcriptPageEnd }} of {{ totalTranscripts }}</span>
              <span v-else>0 conversations</span>
            </span>
            <div class="flex items-center gap-1.5">
              <span class="text-[10px] text-slate-500 font-mono">Show:</span>
              <select v-model="pageSize" @change="onPageSizeChange" class="bg-slate-800 border border-slate-700 text-[10px] rounded px-1.5 py-0.5 text-slate-300 focus:outline-none font-mono">
                <option :value="15">15</option>
                <option :value="30">30</option>
                <option :value="50">50</option>
              </select>
            </div>
          </div>

          <!-- Transcript Cards List -->
          <div class="flex-1 overflow-y-auto p-2 space-y-2">
            <div v-if="transcripts.length === 0 && !loadingTranscripts" class="p-6 text-center text-xs text-slate-500">
              No conversation transcripts archived yet.
            </div>
            <div v-if="loadingTranscripts" class="p-6 text-center text-xs text-slate-500 flex items-center justify-center gap-2">
              <i class="fa-solid fa-spinner fa-spin text-indigo-400"></i> Loading transcripts...
            </div>
            <div v-for="tr in transcripts" :key="tr.transcript_id" @click="selectTranscript(tr)"
              :class="{'border-indigo-500 bg-slate-800/90 shadow-md ring-1 ring-indigo-500/30': selectedTranscriptId === tr.transcript_id, 'border-slate-800/90 bg-slate-800/30 hover:bg-slate-800/60 hover:border-slate-700': selectedTranscriptId !== tr.transcript_id}"
              class="p-2.5 rounded-lg border cursor-pointer transition text-xs space-y-1.5">
              <!-- Top Row: Host badge & Ref / Date -->
              <div class="flex items-center justify-between text-[10px]">
                <div class="flex items-center gap-1.5 font-mono">
                  <span class="px-1.5 py-0.2 rounded bg-indigo-950/90 text-indigo-300 border border-indigo-800/60 font-semibold uppercase text-[9px]">{{ tr.source_host }}</span>
                  <span class="text-slate-400 truncate max-w-[110px]" :title="tr.source_ref">{{ tr.source_ref.slice(0, 8) }}</span>
                </div>
                <span class="text-slate-500 font-mono">{{ tr.captured_at ? tr.captured_at.slice(0, 10) : '' }}</span>
              </div>
              
              <!-- Main Title: Conversation Name / Prompt -->
              <div class="text-slate-200 font-medium text-[12px] leading-snug line-clamp-2" :title="tr.title || 'Untitled conversation'">
                {{ tr.title || 'Untitled conversation' }}
              </div>

              <!-- Bottom Row: Turns & Redactions & Size -->
              <div class="flex items-center justify-between text-[10px] text-slate-400 font-mono pt-1 border-t border-slate-800/50">
                <span class="flex items-center gap-1">
                  <i class="fa-regular fa-comment-dots text-slate-500"></i> {{ tr.turn_count }} turns
                </span>
                <span v-if="tr.redaction_count > 0" class="text-amber-400 font-semibold flex items-center gap-1" :title="tr.redaction_count + ' redacted secrets'">
                  <i class="fa-solid fa-shield-halved"></i> {{ tr.redaction_count }}
                </span>
                <span class="text-slate-500">{{ formatBytes(tr.body_bytes) }}</span>
              </div>
            </div>
          </div>

          <!-- Pagination Bar -->
          <div v-if="totalPages > 1" class="p-2.5 border-t border-slate-800/80 bg-slate-900/90 flex items-center justify-between text-xs shrink-0">
            <button @click="prevPage" :disabled="currentPage <= 1 || loadingTranscripts"
              class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed text-slate-300 transition text-[11px] flex items-center gap-1.5 font-medium">
              <i class="fa-solid fa-chevron-left text-[10px]"></i> Prev
            </button>
            <span class="text-[11px] text-slate-400 font-mono">
              Page <span class="text-slate-200 font-semibold">{{ currentPage }}</span> / {{ totalPages }}
            </span>
            <button @click="nextPage" :disabled="currentPage >= totalPages || loadingTranscripts"
              class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed text-slate-300 transition text-[11px] flex items-center gap-1.5 font-medium">
              Next <i class="fa-solid fa-chevron-right text-[10px]"></i>
            </button>
          </div>
        </div>
      </aside>

      <!-- Middle Column: Active Transcript / Conversation Viewer -->
      <main class="flex-1 min-w-0 flex flex-col bg-slate-950 overflow-hidden">
        <div v-if="activeDoc" class="border-b border-slate-800/80 px-5 py-3 bg-slate-900/50 flex flex-col gap-1.5 shrink-0">
          <div class="flex items-center justify-between">
            <div class="flex items-center space-x-2.5">
              <span class="px-2 py-0.5 text-xs font-mono rounded bg-indigo-950 text-indigo-300 border border-indigo-800/50 font-semibold uppercase">{{ activeDoc.source_host }}</span>
              <span class="text-xs font-mono text-slate-400 select-all" :title="activeDoc.source_ref">ref: {{ activeDoc.source_ref }}</span>
            </div>
            <div class="flex items-center space-x-4 text-xs text-slate-400 font-mono">
              <span><i class="fa-solid fa-comments text-slate-500 mr-1"></i> {{ activeDoc.turns.length }} Turns</span>
              <span v-if="activeDoc.redaction_count > 0" class="text-amber-400">
                <i class="fa-solid fa-shield-halved mr-1"></i> {{ activeDoc.redaction_count }} Redactions
              </span>
            </div>
          </div>
          <h2 class="text-sm font-semibold text-slate-100 truncate mt-0.5" :title="activeTranscriptTitle">
            {{ activeTranscriptTitle || activeDoc.title || 'Conversation Transcript' }}
          </h2>
        </div>

        <div v-if="activeDoc" ref="transcriptContainer" class="flex-1 overflow-y-auto p-6 space-y-6">
          <div v-for="turn in activeDoc.turns" :key="turn.seq" :id="'turn-' + turn.seq" :class="{'bg-slate-900/90 border-slate-700 shadow-lg ring-1 ring-indigo-500/40': highlightedSeq === turn.seq, 'border-slate-800/80 bg-slate-900/30': highlightedSeq !== turn.seq}" class="rounded-xl border p-4 transition-all">
            <!-- Turn Header -->
            <div class="flex items-center justify-between pb-2 mb-3 border-b border-slate-800/60 text-xs">
              <div class="flex items-center space-x-2">
                <span :class="{'bg-blue-950 text-blue-300 border-blue-800': turn.role === 'human', 'bg-purple-950 text-purple-300 border-purple-800': turn.role === 'agent', 'bg-slate-800 text-slate-400 border-slate-700': turn.role === 'system'}" class="px-2 py-0.5 rounded font-mono font-medium uppercase text-[10px] border">
                  {{ turn.role }}
                </span>
                <span class="text-slate-500 font-mono text-[11px]">#{{ turn.seq }}</span>
              </div>
              <div class="text-[10px] text-slate-500 font-mono">
                {{ turn.timestamp ? turn.timestamp.slice(11, 19) : '' }}
              </div>
            </div>

            <!-- Turn Blocks -->
            <div class="space-y-3 text-xs leading-relaxed text-slate-200">
              <div v-for="(block, bIdx) in turn.blocks" :key="bIdx">
                <!-- Text Block -->
                <div v-if="block.type === 'text'" class="whitespace-pre-wrap font-sans leading-normal">
                  {{ block.text }}
                </div>

                <!-- Tool Use Block (Collapsible) -->
                <div v-else-if="block.type === 'tool_use'" class="rounded-lg border border-slate-800 bg-slate-950/70 overflow-hidden my-2">
                  <div @click="block._collapsed = !block._collapsed" class="px-3 py-2 bg-slate-800/40 flex items-center justify-between cursor-pointer hover:bg-slate-800/60 text-slate-300 text-[11px]">
                    <div class="flex items-center space-x-2 font-mono">
                      <i class="fa-solid fa-wrench text-amber-400"></i>
                      <span class="font-semibold text-amber-300">{{ block.name || block.tool_name }}</span>
                    </div>
                    <div class="flex items-center space-x-2 text-[10px] text-slate-400 font-mono">
                      <span>{{ block.input_summary || block.tool_input || 'tool call' }}</span>
                      <i class="fa-solid" :class="block._collapsed ? 'fa-chevron-down' : 'fa-chevron-up'"></i>
                    </div>
                  </div>
                  <div v-if="!block._collapsed" class="p-3 bg-slate-950 font-mono text-[11px] overflow-x-auto text-slate-300 border-t border-slate-800/60">
                    <pre>{{ JSON.stringify(block.input || block.tool_input, null, 2) }}</pre>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div v-else class="flex-1 flex flex-col items-center justify-center text-slate-500 space-y-3 p-8 text-center">
          <div class="w-16 h-16 rounded-full bg-slate-900 border border-slate-800 flex items-center justify-center text-2xl text-slate-600">
            <i class="fa-solid fa-comments"></i>
          </div>
          <p class="text-sm">Select a transcript or search hit on the left to inspect conversation turns.</p>
        </div>
      </main>

      <!-- Right Column: Active Session / Checkpoint Details -->
      <aside v-if="selectedSession && showSessionPanel" class="w-80 min-w-[260px] bg-slate-900/90 border-l border-slate-800 flex flex-col overflow-y-auto shrink-0 p-4 space-y-4 text-xs">
        <div class="flex items-center justify-between pb-3 border-b border-slate-800">
          <h2 class="font-semibold text-slate-200 flex items-center gap-1.5">
            <i class="fa-solid fa-cube text-indigo-400"></i> Session Details
          </h2>
          <span :class="{'bg-emerald-950 text-emerald-300 border-emerald-800': selectedSession.status === 'open', 'bg-amber-950 text-amber-300 border-amber-800': selectedSession.status === 'parked', 'bg-slate-800 text-slate-400 border-slate-700': selectedSession.status === 'closed'}" class="px-2 py-0.5 rounded text-[10px] font-mono uppercase border font-semibold">
            {{ selectedSession.status }}
          </span>
        </div>

        <!-- Identity & Metadata -->
        <div class="space-y-2 text-[11px] font-mono text-slate-300 bg-slate-950/60 p-3 rounded-lg border border-slate-800">
          <div><span class="text-slate-500">ID:</span> {{ selectedSession.session_id }}</div>
          <div><span class="text-slate-500">Agent:</span> {{ selectedSession.agent_id }}</div>
          <div><span class="text-slate-500">Operator:</span> {{ selectedSession.operator_id }}</div>
          <div><span class="text-slate-500">Title:</span> {{ selectedSession.title || 'Untitled' }}</div>
          <div v-if="selectedSession.parent_session_id"><span class="text-slate-500">Parent:</span> {{ selectedSession.parent_session_id.slice(0, 8) }}...</div>
          <div v-if="selectedSession.fork_reason"><span class="text-slate-500">Fork Reason:</span> <span class="text-indigo-300">{{ selectedSession.fork_reason }}</span></div>
        </div>

        <!-- Copy Command Card -->
        <div class="p-3 bg-gradient-to-br from-indigo-950/60 to-slate-900 rounded-lg border border-indigo-800/40 space-y-2">
          <div class="text-[11px] font-semibold text-indigo-300 flex items-center justify-between">
            <span>Continue in IDE</span>
            <button @click="copyCliCommand" class="text-xs text-indigo-400 hover:text-indigo-200">
              <i class="fa-regular fa-copy"></i>
            </button>
          </div>
          <div class="bg-slate-950/80 p-2 rounded text-[10px] font-mono text-slate-300 overflow-x-auto select-all">
            agentloom-session open --fork-from {{ selectedSession.session_id }}
          </div>
        </div>

        <!-- Checkpoints List -->
        <div class="space-y-2">
          <h3 class="font-semibold text-slate-300 flex items-center justify-between">
            <span>Checkpoints ({{ sessionCheckpoints.length }})</span>
          </h3>
          <div v-for="cp in sessionCheckpoints" :key="cp.checkpoint_id" class="p-3 bg-slate-950/40 rounded-lg border border-slate-800 space-y-2 text-[11px]">
            <div class="flex items-center justify-between text-[10px] text-slate-500 font-mono">
              <span>{{ cp.created_at ? cp.created_at.slice(0, 16).replace('T', ' ') : '' }}</span>
              <span class="truncate max-w-[90px]">{{ cp.vcs_branch || 'branch' }}</span>
            </div>
            <div v-if="cp.next_action" class="text-emerald-400 font-medium">
              <span class="text-slate-500 font-normal">Next:</span> {{ cp.next_action }}
            </div>
            <div v-if="cp.open_plan_path" class="text-indigo-300 font-mono text-[10px] truncate">
              <span class="text-slate-500">Plan:</span> {{ cp.open_plan_path }}
            </div>
            <div v-if="cp.decisions && cp.decisions.length" class="space-y-1">
              <span class="text-[10px] text-slate-500">Decisions:</span>
              <ul class="list-disc list-inside text-slate-300 text-[10px] space-y-0.5">
                <li v-for="(dec, dIdx) in cp.decisions" :key="dIdx">{{ dec }}</li>
              </ul>
            </div>
          </div>
        </div>
      </aside>
    </div>
  </div>

  <script>
    const { createApp, ref, computed, onMounted, nextTick } = Vue;

    const TreeNodeComponent = {
      name: 'tree-node',
      props: ['node', 'selectedId'],
      emits: ['select'],
      template: `
        <div class="space-y-1">
          <div @click="$emit('select', node)" :class="{'border-indigo-500 bg-slate-800/80': selectedId === node.session_id, 'border-slate-800 bg-slate-800/20 hover:bg-slate-800/50': selectedId !== node.session_id}" class="p-2 rounded-lg border cursor-pointer transition text-xs space-y-1">
            <div class="flex items-center justify-between">
              <span class="font-mono text-[11px] text-slate-200 font-semibold">{{ node.session_id.slice(0, 8) }}..</span>
              <span :class="{'bg-emerald-950 text-emerald-300 border-emerald-800': node.status === 'open', 'bg-amber-950 text-amber-300 border-amber-800': node.status === 'parked', 'bg-slate-800 text-slate-400 border-slate-700': node.status === 'closed'}" class="px-1.5 py-0.2 rounded text-[9px] font-mono uppercase border">
                {{ node.status }}
              </span>
            </div>
            <div class="text-[11px] text-slate-300 truncate">{{ node.title || 'Untitled session' }}</div>
            <div class="flex items-center justify-between text-[10px] text-slate-500 font-mono">
              <span>{{ node.agent_id }}</span>
              <span v-if="node.fork_reason" class="text-indigo-400">fork: {{ node.fork_reason }}</span>
            </div>
          </div>
          <div v-if="node.children && node.children.length" class="pl-3 border-l border-slate-800 space-y-1 ml-2">
            <tree-node v-for="child in node.children" :key="child.session_id" :node="child" :selected-id="selectedId" @select="$emit('select', $event)"></tree-node>
          </div>
        </div>
      `
    };

    const app = createApp({
      setup() {
        const workspaces = ref([]);
        const selectedWorkspace = ref('');
        const leftTab = ref('transcripts');
        const sessionTree = ref([]);
        const transcripts = ref([]);
        const totalTranscripts = ref(0);
        const currentPage = ref(1);
        const pageSize = ref(15);
        const loadingTranscripts = ref(false);

        const selectedSessionId = ref(null);
        const selectedSession = ref(null);
        const sessionCheckpoints = ref([]);
        const selectedTranscriptId = ref(null);
        const activeDoc = ref(null);
        const highlightedSeq = ref(null);
        const showSessionPanel = ref(true);
        const searchQuery = ref('');
        const searchResults = ref(null);
        const loading = ref(false);
        const transcriptContainer = ref(null);

        const totalPages = computed(() => Math.max(1, Math.ceil(totalTranscripts.value / pageSize.value)));
        const transcriptPageStart = computed(() => totalTranscripts.value === 0 ? 0 : (currentPage.value - 1) * pageSize.value + 1);
        const transcriptPageEnd = computed(() => Math.min(totalTranscripts.value, currentPage.value * pageSize.value));
        const activeTranscriptTitle = computed(() => {
          const tr = transcripts.value.find(t => t.transcript_id === selectedTranscriptId.value);
          return (tr && tr.title) ? tr.title : (activeDoc.value ? (activeDoc.value.title || activeDoc.value.source_ref) : '');
        });

        const formatBytes = (bytes) => {
          if (!bytes || bytes <= 0) return '0 B';
          const k = 1024;
          const sizes = ['B', 'KB', 'MB', 'GB'];
          const i = Math.floor(Math.log(bytes) / Math.log(k));
          return (bytes / Math.pow(k, i)).toFixed(1) + ' ' + sizes[i];
        };

        const fetchWorkspaces = async () => {
          try {
            const res = await fetch('/api/workspaces');
            workspaces.value = await res.json();
            if (workspaces.value.length && !selectedWorkspace.value) {
              selectedWorkspace.value = workspaces.value[0];
              await loadWorkspaceData();
            }
          } catch (e) {
            console.error('Failed to load workspaces:', e);
          }
        };

        const fetchTranscripts = async (page = 1) => {
          if (!selectedWorkspace.value) return;
          loadingTranscripts.value = true;
          currentPage.value = page;
          const offset = (page - 1) * pageSize.value;
          try {
            const res = await fetch(
              '/api/transcripts?workspace=' + encodeURIComponent(selectedWorkspace.value) +
              '&limit=' + pageSize.value + '&offset=' + offset
            );
            const data = await res.json();
            if (Array.isArray(data)) {
              transcripts.value = data;
              totalTranscripts.value = data.length;
            } else {
              transcripts.value = data.items || [];
              totalTranscripts.value = data.total !== undefined ? data.total : (data.items || []).length;
            }
            if (transcripts.value.length && (!activeDoc.value || !selectedTranscriptId.value)) {
              selectTranscript(transcripts.value[0]);
            }
          } catch (e) {
            console.error('Failed to load transcripts:', e);
          } finally {
            loadingTranscripts.value = false;
          }
        };

        const loadWorkspaceData = async () => {
          if (!selectedWorkspace.value) return;
          loading.value = true;
          try {
            const treePromise = fetch('/api/sessions?workspace=' + encodeURIComponent(selectedWorkspace.value))
              .then(r => r.json())
              .then(tree => {
                sessionTree.value = tree;
                if (sessionTree.value.length && !selectedSession.value) {
                  selectSession(sessionTree.value[0]);
                }
              });
            const trPromise = fetchTranscripts(1);
            await Promise.all([treePromise, trPromise]);
          } catch (e) {
            console.error('Failed to load data:', e);
          } finally {
            loading.value = false;
          }
        };

        const nextPage = () => {
          if (currentPage.value < totalPages.value) {
            fetchTranscripts(currentPage.value + 1);
          }
        };

        const prevPage = () => {
          if (currentPage.value > 1) {
            fetchTranscripts(currentPage.value - 1);
          }
        };

        const onPageSizeChange = () => {
          fetchTranscripts(1);
        };

        const onWorkspaceChange = () => {
          activeDoc.value = null;
          selectedSession.value = null;
          selectedTranscriptId.value = null;
          searchResults.value = null;
          currentPage.value = 1;
          loadWorkspaceData();
        };

        const selectSession = async (node) => {
          selectedSessionId.value = node.session_id;
          selectedSession.value = node;
          try {
            const res = await fetch('/api/sessions/' + node.session_id + '/checkpoints');
            sessionCheckpoints.value = await res.json();
          } catch (e) {
            sessionCheckpoints.value = [];
          }
        };

        const selectTranscript = async (tr) => {
          selectedTranscriptId.value = tr.transcript_id;
          try {
            const res = await fetch('/api/transcripts/' + tr.transcript_id);
            const doc = await res.json();
            doc.turns.forEach(t => {
              t.blocks.forEach(b => {
                if (b.type === 'tool_use') b._collapsed = true;
              });
            });
            activeDoc.value = doc;
          } catch (e) {
            console.error('Failed to load transcript:', e);
          }
        };

        const performSearch = async () => {
          if (!searchQuery.value.trim()) return;
          loading.value = true;
          try {
            const res = await fetch('/api/search?q=' + encodeURIComponent(searchQuery.value) + '&workspace=' + encodeURIComponent(selectedWorkspace.value));
            searchResults.value = await res.json();
          } catch (e) {
            console.error('Search failed:', e);
          } finally {
            loading.value = false;
          }
        };

        const clearSearch = () => {
          searchQuery.value = '';
          searchResults.value = null;
          highlightedSeq.value = null;
        };

        const selectSearchHit = async (hit) => {
          const tr = transcripts.value.find(t => t.source_ref === hit.source_ref || t.transcript_id === hit.transcript_id);
          if (tr) {
            await selectTranscript(tr);
          } else {
            const res = await fetch('/api/transcripts/' + hit.transcript_id);
            activeDoc.value = await res.json();
          }
          highlightedSeq.value = hit.seq_start;
          await nextTick();
          const el = document.getElementById('turn-' + hit.seq_start);
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
        };

        const copyCliCommand = () => {
          if (!selectedSession.value) return;
          const cmd = 'agentloom-session open --fork-from ' + selectedSession.value.session_id;
          navigator.clipboard.writeText(cmd);
          alert('Copied to clipboard: ' + cmd);
        };

        const refreshAll = () => {
          loadWorkspaceData();
        };

        onMounted(() => {
          fetchWorkspaces();
        });

        return {
          workspaces,
          selectedWorkspace,
          leftTab,
          sessionTree,
          transcripts,
          totalTranscripts,
          currentPage,
          pageSize,
          totalPages,
          transcriptPageStart,
          transcriptPageEnd,
          activeTranscriptTitle,
          loadingTranscripts,
          selectedSessionId,
          selectedSession,
          sessionCheckpoints,
          selectedTranscriptId,
          activeDoc,
          highlightedSeq,
          showSessionPanel,
          searchQuery,
          searchResults,
          loading,
          transcriptContainer,
          formatBytes,
          fetchTranscripts,
          nextPage,
          prevPage,
          onPageSizeChange,
          onWorkspaceChange,
          selectSession,
          selectTranscript,
          performSearch,
          clearSearch,
          selectSearchHit,
          copyCliCommand,
          refreshAll
        };
      }
    });

    app.component('tree-node', TreeNodeComponent);
    app.mount('#app');
  </script>
</body>
</html>
"""


def _int_param(
    query: dict[str, list[str]],
    name: str,
    default: int,
    *,
    maximum: int,
    minimum: int = 1,
) -> int:
    """Read an integer from the query string with clamping.

    Clamped rather than rejected: this viewer is a local read-only tool, and a
    hand-typed URL should degrade to a sane page instead of an error. The upper
    bound keeps one request from pulling the whole archive into a browser tab.
    """
    raw = query.get(name, [None])[0]
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


class SessionApiHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            return

        if path == "/api/workspaces":
            self._handle_workspaces()
            return

        if path == "/api/sessions":
            ws = query.get("workspace", [None])[0]
            self._handle_sessions(ws)
            return

        if path.startswith("/api/sessions/") and path.endswith("/checkpoints"):
            session_id = path.split("/")[3]
            self._handle_session_checkpoints(session_id)
            return

        if path.startswith("/api/sessions/") and path.endswith("/lineage"):
            session_id = path.split("/")[3]
            self._handle_session_lineage(session_id)
            return

        if path == "/api/transcripts":
            ws = query.get("workspace", [None])[0]
            limit = _int_param(query, "limit", 20, maximum=500, minimum=1)
            offset = _int_param(query, "offset", 0, maximum=100000, minimum=0)
            self._handle_transcripts(ws, limit, offset)
            return

        if path.startswith("/api/transcripts/"):
            transcript_id = path.split("/")[3]
            self._handle_transcript_detail(transcript_id)
            return

        if path == "/api/search":
            q = query.get("q", [""])[0]
            ws = query.get("workspace", [None])[0]
            lexical = query.get("lexical", ["0"])[0] in ("1", "true", "yes")
            self._handle_search(q, ws, _int_param(query, "limit", 12, maximum=100, minimum=1), lexical)
            return

        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "not found"}).encode("utf-8"))

    def _json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def _handle_workspaces(self) -> None:
        sessions = store.search_sessions(limit=200)
        ws_set = {s.workspace_key for s in sessions if s.workspace_key}
        if not ws_set:
            try:
                local_ws = detect_workspace_key()
                if local_ws:
                    ws_set.add(local_ws)
            except Exception:
                pass
        self._json(sorted(ws_set))

    def _handle_sessions(self, workspace_key: Optional[str]) -> None:
        if not workspace_key:
            self._json([])
            return
        roots = store.get_workspace_session_tree(workspace_key)
        self._json(roots)

    def _handle_session_checkpoints(self, session_id: str) -> None:
        cps = store.list_checkpoints(session_id, limit=20)
        self._json(cps)

    def _handle_session_lineage(self, session_id: str) -> None:
        try:
            lineage = store.get_session_lineage(session_id)
            self._json(lineage)
        except Exception as exc:
            self._json({"error": str(exc)}, status=404)

    def _handle_transcripts(
        self, workspace_key: Optional[str], limit: int = 20, offset: int = 0
    ) -> None:
        try:
            total = store.count_transcripts(workspace_key=workspace_key)
        except Exception:
            total = 0
        trs = store.list_transcripts(
            workspace_key=workspace_key, limit=limit, offset=offset
        )
        if total == 0 and trs:
            total = len(trs)
        self._json({
            "items": [t.to_dict() for t in trs],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    def _handle_transcript_detail(self, transcript_id: str) -> None:
        doc = store.load_transcript(transcript_id=transcript_id)
        if doc is None:
            self._json({"error": "transcript not found"}, status=404)
            return
        self._json(doc.to_dict())

    def _handle_search(
        self,
        query: str,
        workspace_key: Optional[str],
        limit: int = 12,
        lexical_only: bool = False,
    ) -> None:
        if not query.strip():
            self._json([])
            return

        # Without a query vector the archive's embeddings are never consulted
        # and the viewer silently returns lexical-only results.
        from agentloom_runtime.memory.embedding_provider import embed_query, get_embedding_model

        model = get_embedding_model()
        query_vec = None if lexical_only else embed_query(query, model=model)

        hits = store.search_archive(
            query,
            workspace_key=workspace_key,
            limit=limit,
            query_vec=query_vec,
            model=model,
        )
        self._json([h.to_dict() for h in hits])

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default noisy access logs


def run_server(host: str = "127.0.0.1", port: int = 8766, open_browser: bool = True) -> int:
    """Launch the Layer 0 Session Viewer web server."""
    with socketserver.ThreadingTCPServer((host, port), SessionApiHandler) as httpd:
        url = f"http://{host}:{port}"
        print(f"AgentLoom Layer 0 Session Viewer running at: {url}")
        print("Press Ctrl+C to stop.")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down Layer 0 Session Viewer.")
            return 0
    return 0
