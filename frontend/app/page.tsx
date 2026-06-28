"use client";

import React, { useState, useEffect, useRef } from "react";

interface Lead {
  _id?: string;
  thread_id: string;
  company?: string;
  job_title?: string;
  intent_score?: number;
  status?: string;
  fit?: boolean;
  handoff_reason?: string;
}

interface Message {
  role: "user" | "assistant";
  content: string;
  thought?: string;
}

interface ToolCall {
  tool: string;
  inputs: any;
  output?: string;
  status: "running" | "completed";
}

export default function Dashboard() {
  const [threadId, setThreadId] = useState<string>("");
  const [threads, setThreads] = useState<Array<{thread_id: string, title?: string}>>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [activeLead, setActiveLead] = useState<Lead | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [activeTab, setActiveTab] = useState<"sandbox" | "supervisor" | "leads">("sandbox");
  
  // Real-time states
  const [connected, setConnected] = useState<boolean>(false);
  const [statusText, setStatusText] = useState<string>("Disconnected");
  const [streamingThought, setStreamingThought] = useState<string>("");
  const [streamingResponse, setStreamingResponse] = useState<string>("");
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  
  // Extension Settings (v2)
  const [apiKey, setApiKey] = useState<string>("test_key_abc123");
  const [voiceEnabled, setVoiceEnabled] = useState<boolean>(false);
  const voiceEnabledRef = useRef(false);
  
  // Inputs
  const [chatInput, setChatInput] = useState<string>("");
  const [supervisorMessage, setSupervisorMessage] = useState<string>("");
  const [selectedHandoffThread, setSelectedHandoffThread] = useState<string>("");
  
  // Refs to prevent closure stale states
  const ws = useRef<WebSocket | null>(null);
  const streamingThoughtRef = useRef("");
  const streamingResponseRef = useRef("");
  const toolCallsRef = useRef<ToolCall[]>([]);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const supervisorChatEndRef = useRef<HTMLDivElement>(null);

  // Sync voice toggle with ref
  useEffect(() => {
    voiceEnabledRef.current = voiceEnabled;
  }, [voiceEnabled]);

  // Load saved API Key from localStorage on mount
  useEffect(() => {
    if (typeof window !== "undefined") {
      const savedKey = localStorage.getItem("sdr_api_key");
      if (savedKey) {
        setApiKey(savedKey);
      }
    }
  }, []);

  const handleApiKeyChange = (val: string) => {
    setApiKey(val);
    if (typeof window !== "undefined") {
      localStorage.setItem("sdr_api_key", val);
    }
  };

  const getHeaders = () => {
    return {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    };
  };

  // Speak assistant response using native Web Speech Synthesis
  const speakResponse = (text: string) => {
    if (typeof window !== "undefined" && window.speechSynthesis) {
      window.speechSynthesis.cancel();
      // Remove any thought markdown blocks and clean standard formatting
      const clean = text
        .replace(/<thought>[\s\S]*?<\/thought>/gi, "")
        .replace(/[*_`#~]/g, "")
        .trim();
      
      if (!clean) return;
      
      const utterance = new SpeechSynthesisUtterance(clean);
      window.speechSynthesis.speak(utterance);
    }
  };

  // Initialize a random thread if none exists
  useEffect(() => {
    fetchThreads();
    fetchLeads();
    
    const randomId = "thread_" + Math.random().toString(36).substring(2, 10);
    setThreadId(randomId);
  }, [apiKey]); // Refetch if API key changes

  // Fetch threads and leads lists
  const fetchThreads = async () => {
    try {
      const res = await fetch("http://localhost:8008/api/conversations", { headers: getHeaders() });
      if (res.ok) {
        const data = await res.json();
        setThreads(data);
      } else if (res.status === 401) {
        setStatusText("Error: Invalid API Key (401)");
      }
    } catch (e) {
      console.error("Error fetching threads:", e);
    }
  };

  const handleRenameThread = async (id: string, currentTitle: string) => {
    const newTitle = window.prompt("Rename chat thread:", currentTitle);
    if (!newTitle || newTitle.trim() === "") return;
    try {
      const res = await fetch(`http://localhost:8008/api/conversations/${id}/title`, {
        method: "PUT",
        headers: {
          ...getHeaders(),
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ title: newTitle.trim() })
      });
      if (res.ok) {
        fetchThreads();
      }
    } catch (e) {
      console.error("Error renaming thread:", e);
    }
  };

  const handleDeleteThread = async (id: string) => {
    const confirmed = window.confirm("Are you sure you want to delete this chat session? This will remove all conversation logs, leads status, and SDR agent memory.");
    if (!confirmed) return;
    try {
      const res = await fetch(`http://localhost:8008/api/conversations/${id}`, {
        method: "DELETE",
        headers: getHeaders()
      });
      if (res.ok) {
        fetchThreads();
        fetchLeads();
        if (threadId === id) {
          const nextThread = threads.find((t) => t.thread_id !== id);
          if (nextThread) {
            setThreadId(nextThread.thread_id);
          } else {
            const randomId = "thread_" + Math.random().toString(36).substring(2, 10);
            setThreadId(randomId);
          }
        }
      }
    } catch (e) {
      console.error("Error deleting thread:", e);
    }
  };

  const fetchLeads = async () => {
    try {
      const res = await fetch("http://localhost:8008/api/leads", { headers: getHeaders() });
      if (res.ok) {
        const data = await res.json();
        setLeads(data);
      }
    } catch (e) {
      console.error("Error fetching leads:", e);
    }
  };

  const fetchLeadProfile = async (id: string) => {
    try {
      const res = await fetch(`http://localhost:8008/api/leads/${id}`, { headers: getHeaders() });
      if (res.ok) {
        const data = await res.json();
        setActiveLead(data);
      } else {
        setActiveLead(null);
      }
    } catch (e) {
      setActiveLead(null);
    }
  };

  // Scroll views
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingResponse, streamingThought, toolCalls]);

  useEffect(() => {
    supervisorChatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Handle WebSocket Connection
  useEffect(() => {
    if (!threadId || !apiKey) return;

    setMessages([]);
    setStreamingThought("");
    setStreamingResponse("");
    setToolCalls([]);
    streamingThoughtRef.current = "";
    streamingResponseRef.current = "";
    toolCallsRef.current = [];

    // Connect to WebSocket passing api_key in query string for authentication
    const socket = new WebSocket(`ws://localhost:8008/ws/chat/${threadId}?api_key=${apiKey}`);
    
    socket.onopen = () => {
      setConnected(true);
      setStatusText("Connected. Idle.");
    };
    
    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      
      if (data.type === "unauthorized") {
        setStatusText("Error: API Key Unauthorized");
        setConnected(false);
        socket.close();
        return;
      }
      
      if (data.type === "history") {
        setMessages(data.messages);
      } else if (data.type === "lead_status") {
        setActiveLead(data.lead);
        fetchLeads();
      } else if (data.type === "status") {
        setStatusText(data.status);
        if (data.status === "Idle") {
          const finalResponse = streamingResponseRef.current;
          const finalThought = streamingThoughtRef.current;
          
          if (finalResponse || finalThought) {
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant",
                content: finalResponse,
                thought: finalThought || undefined
              }
            ]);
            
            // Speak complete final response if TTS Voice Mode is toggled ON
            if (voiceEnabledRef.current && finalResponse) {
              speakResponse(finalResponse);
            }
            
            streamingResponseRef.current = "";
            streamingThoughtRef.current = "";
            toolCallsRef.current = [];
            setStreamingThought("");
            setStreamingResponse("");
            setToolCalls([]);
          }
        }
      } else if (data.type === "thought") {
        streamingThoughtRef.current += data.token;
        setStreamingThought(streamingThoughtRef.current);
      } else if (data.type === "response") {
        streamingResponseRef.current += data.token;
        setStreamingResponse(streamingResponseRef.current);
      } else if (data.type === "tool_start") {
        const updated = [...toolCallsRef.current, { tool: data.tool, inputs: data.inputs, status: "running" as const }];
        toolCallsRef.current = updated;
        setToolCalls(updated);
      } else if (data.type === "tool_end") {
        const updated = toolCallsRef.current.map((t) => 
          t.tool === data.tool && t.status === "running" 
            ? { ...t, output: data.output, status: "completed" as const } 
            : t
        );
        toolCallsRef.current = updated;
        setToolCalls(updated);
      } else if (data.type === "human_response") {
        setMessages((prev) => [...prev, { role: "assistant", content: data.message }]);
        if (voiceEnabledRef.current && data.message) {
          speakResponse(data.message);
        }
      } else if (data.type === "handoff") {
        setStatusText(`Handoff requested: ${data.reason}`);
        fetchLeads();
      } else if (data.type === "error") {
        setStatusText(`Error: ${data.message}`);
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: `⚠️ Error: ${data.message}`
          }
        ]);
        streamingResponseRef.current = "";
        streamingThoughtRef.current = "";
        toolCallsRef.current = [];
        setStreamingThought("");
        setStreamingResponse("");
        setToolCalls([]);
      }
    };
    
    socket.onclose = () => {
      setConnected(false);
      setStatusText("Disconnected");
    };
    
    socket.onerror = () => {
      setConnected(false);
      setStatusText("Connection error");
    };
    
    ws.current = socket;
    fetchLeadProfile(threadId);

    setThreads((prev) => (prev.includes(threadId) ? prev : [...prev, threadId]));

    return () => {
      socket.close();
    };
  }, [threadId, apiKey]);

  // Send message as Sandbox user
  const handleSendMessage = () => {
    if (!chatInput.trim() || !ws.current || ws.current.readyState !== WebSocket.OPEN) return;
    
    const userMsg = chatInput.trim();
    setMessages((prev) => [...prev, { role: "user", content: userMsg }]);
    
    ws.current.send(JSON.stringify({ message: userMsg }));
    setChatInput("");
    setStreamingThought("");
    setStreamingResponse("");
    setToolCalls([]);
    streamingThoughtRef.current = "";
    streamingResponseRef.current = "";
    toolCallsRef.current = [];
  };

  // Create new sandbox chat thread
  const handleCreateNewThread = () => {
    const newId = "thread_" + Math.random().toString(36).substring(2, 10);
    setThreadId(newId);
    setActiveTab("sandbox");
  };

  // Supervisor actions
  const handleClaimThread = async (id: string) => {
    try {
      const res = await fetch(`http://localhost:8008/api/handoffs/${id}/claim`, { method: "POST", headers: getHeaders() });
      if (res.ok) {
        fetchLeads();
        fetchLeadProfile(id);
      }
    } catch (e) {
      console.error(e);
    }
  };

  const handleResolveThread = async (id: string) => {
    try {
      const res = await fetch(`http://localhost:8008/api/handoffs/${id}/resolve`, { method: "POST", headers: getHeaders() });
      if (res.ok) {
        fetchLeads();
        fetchLeadProfile(id);
      }
    } catch (e) {
      console.error(e);
    }
  };

  const handleSendSupervisorMessage = async (id: string) => {
    if (!supervisorMessage.trim()) return;
    try {
      const res = await fetch(`http://localhost:8008/api/handoffs/${id}/message`, {
        method: "POST",
        headers: getHeaders(),
        body: JSON.stringify({ message: supervisorMessage.trim() })
      });
      if (res.ok) {
        setMessages((prev) => [...prev, { role: "assistant", content: supervisorMessage.trim(), thought: "Sent by human operator" }]);
        setSupervisorMessage("");
      }
    } catch (e) {
      console.error(e);
    }
  };

  const renderStatusBadge = (status?: string) => {
    const s = status || "New";
    let bg = "bg-gray-800 text-gray-400 border-gray-700";
    let pulse = false;

    if (s === "Qualified") bg = "bg-emerald-950/60 text-emerald-400 border-emerald-800/80";
    else if (s === "Unqualified") bg = "bg-rose-950/60 text-rose-400 border-rose-800/80";
    else if (s === "Handoff Requested") {
      bg = "bg-amber-950/60 text-amber-400 border-amber-800/80 animate-pulse";
      pulse = true;
    } else if (s === "Human Claimed") bg = "bg-purple-950/60 text-purple-400 border-purple-800/80";
    else if (s === "Demo Scheduled") bg = "bg-cyan-950/60 text-cyan-400 border-cyan-800/80";

    return (
      <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold border ${bg}`}>
        {pulse && <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-ping" />}
        {s}
      </span>
    );
  };

  return (
    <div className="flex h-screen overflow-hidden bg-[#0A0E1A] text-[#E2E8F0]">
      {/* Sidebar */}
      <aside className="w-80 bg-[#111726] border-r border-[#1F293D] flex flex-col shrink-0">
        {/* Branding */}
        <div className="p-6 border-b border-[#1F293D] flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-lg bg-gradient-to-tr from-sky-500 to-indigo-600 flex items-center justify-center font-bold text-white shadow-md shadow-indigo-500/20">
              S
            </div>
            <div>
              <h1 className="font-extrabold text-sm tracking-wide text-white">SAASFLOW AI</h1>
              <span className="text-[10px] text-slate-400 font-medium">B2B SDR AGENT CONSOLE</span>
            </div>
          </div>
          {/* Glowing Status Dot */}
          <div className="flex items-center gap-1.5 bg-[#1A2333] px-2.5 py-1 rounded-full border border-slate-700">
            <span className={`h-2.5 w-2.5 rounded-full ${connected ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.5)]" : "bg-rose-500"}`} />
            <span className="text-[10px] font-bold uppercase tracking-wider text-slate-300">
              {connected ? "LIVE" : "OFF"}
            </span>
          </div>
        </div>

        {/* API Key Panel (v2) */}
        <div className="p-4 border-b border-[#1F293D] bg-[#161F30]/20">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
              API Authorization Key
            </label>
            <span className="text-[9px] font-bold text-emerald-400 uppercase">Secure</span>
          </div>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => handleApiKeyChange(e.target.value)}
            placeholder="Enter API key to unlock..."
            className="w-full bg-[#1A2234] border border-[#2D3D54] rounded-lg px-3 py-1.5 text-xs text-[#F1F5F9] focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 transition-all font-mono"
          />
        </div>

        {/* Action Button */}
        <div className="p-4">
          <button
            onClick={handleCreateNewThread}
            className="w-full flex items-center justify-center gap-2 py-2.5 px-4 bg-gradient-to-r from-sky-500 to-indigo-600 hover:from-sky-400 hover:to-indigo-500 text-white text-sm font-semibold rounded-lg shadow-md shadow-indigo-600/10 transition-all active:scale-[0.98]"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
            </svg>
            New Lead Chat
          </button>
        </div>

        {/* Thread Tabs list */}
        <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-1">
          <div className="text-[11px] font-bold text-slate-400 uppercase tracking-wider px-2 mb-2">
            Active Chat Threads ({threads.length})
          </div>
          {threads.length === 0 ? (
            <div className="text-center text-xs text-slate-500 py-6 border border-dashed border-slate-800 rounded-lg">
              No threads found. Start a new chat!
            </div>
          ) : (
            threads.map((thread) => {
              const id = thread.thread_id;
              const displayName = thread.title || id;
              return (
                <div
                  key={id}
                  className={`group w-full flex items-center justify-between rounded-lg text-xs font-semibold transition-all border ${
                    threadId === id
                      ? "bg-[#1E293B] text-sky-400 border-sky-500/30 shadow-[0_4px_12px_rgba(0,0,0,0.1)]"
                      : "bg-[#161F30]/40 text-slate-300 hover:bg-[#1E293B]/60 border-transparent"
                  }`}
                >
                  <button
                    onClick={() => {
                      setThreadId(id);
                      if (activeTab === "supervisor") {
                        setSelectedHandoffThread(id);
                      }
                    }}
                    className="flex-1 text-left px-3 py-2.5 truncate"
                  >
                    <div className="flex items-center gap-2">
                      <span className="truncate">{displayName}</span>
                      {leads.find((l) => l.thread_id === id)?.status === "Handoff Requested" && (
                        <span className="h-2 w-2 rounded-full bg-amber-400 animate-ping shrink-0" />
                      )}
                    </div>
                  </button>
                  <div className="hidden group-hover:flex items-center gap-1.5 pr-2 shrink-0">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleRenameThread(id, displayName);
                      }}
                      className="text-slate-500 hover:text-sky-400 p-1 rounded hover:bg-slate-800/50 transition-colors"
                      title="Rename Chat"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                      </svg>
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDeleteThread(id);
                      }}
                      className="text-slate-500 hover:text-rose-400 p-1 rounded hover:bg-slate-800/50 transition-colors"
                      title="Delete Chat"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-9v6M1 4h22M4 4h16" />
                      </svg>
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </aside>

      {/* Main Workspace */}
      <main className="flex-1 flex flex-col overflow-hidden bg-[#0C1220]">
        {/* Navigation Tabs Header */}
        <header className="h-16 border-b border-[#1F293D] px-8 bg-[#111726]/40 backdrop-blur-md flex items-center justify-between shrink-0">
          <nav className="flex gap-4">
            <button
              onClick={() => setActiveTab("sandbox")}
              className={`px-4 py-2 text-xs font-bold uppercase tracking-wider rounded-lg transition-all ${
                activeTab === "sandbox"
                  ? "bg-[#1E293B] text-sky-400 border border-sky-500/20"
                  : "text-slate-400 hover:text-white"
              }`}
            >
              Agent Chat Sandbox
            </button>
            <button
              onClick={() => {
                setActiveTab("supervisor");
                setSelectedHandoffThread(threadId);
              }}
              className={`relative px-4 py-2 text-xs font-bold uppercase tracking-wider rounded-lg transition-all ${
                activeTab === "supervisor"
                  ? "bg-[#1E293B] text-sky-400 border border-sky-500/20"
                  : "text-slate-400 hover:text-white"
              }`}
            >
              Supervisor Console
              {leads.some((l) => l.status === "Handoff Requested") && (
                <span className="absolute -top-1 -right-1 flex h-4 w-4 items-center justify-center rounded-full bg-amber-500 text-[8px] font-black text-black">
                  !
                </span>
              )}
            </button>
            <button
              onClick={() => {
                setActiveTab("leads");
                fetchLeads();
              }}
              className={`px-4 py-2 text-xs font-bold uppercase tracking-wider rounded-lg transition-all ${
                activeTab === "leads"
                  ? "bg-[#1E293B] text-sky-400 border border-sky-500/20"
                  : "text-slate-400 hover:text-white"
              }`}
            >
              CRM Synced Leads
            </button>
          </nav>

          {/* Voice Mode & Refresh Panel (v2) */}
          <div className="flex items-center gap-3">
            <button
              onClick={() => setVoiceEnabled(!voiceEnabled)}
              className={`px-3.5 py-1.5 rounded-lg border text-xs font-bold uppercase tracking-wider transition-all duration-300 flex items-center gap-2 ${
                voiceEnabled
                  ? "bg-emerald-950/60 text-emerald-400 border-emerald-800/80 shadow-[0_0_8px_rgba(52,211,153,0.15)]"
                  : "bg-slate-800/50 text-slate-400 border-slate-700"
              }`}
            >
              {voiceEnabled ? (
                <>
                  <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
                  🔊 Voice Agent: ON
                </>
              ) : (
                <>
                  <span className="h-2 w-2 rounded-full bg-slate-500" />
                  🔇 Voice Agent: OFF
                </>
              )}
            </button>

            <div className="flex items-center gap-3">
              <div className="text-right">
                <div className="text-[10px] text-slate-400 font-bold uppercase tracking-wider">Agent Engine Status</div>
                <div className="text-xs font-semibold text-slate-200">{statusText}</div>
              </div>
              <button
                onClick={() => {
                  fetchThreads();
                  fetchLeads();
                  if (threadId) fetchLeadProfile(threadId);
                }}
                className="p-2 hover:bg-[#1E293B] rounded-lg transition-all border border-slate-700 text-slate-400 hover:text-white"
                title="Refresh lists"
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 1121.21 7.89M9 11l3-3m0 0l3 3m-3-3v12" />
                </svg>
              </button>
            </div>
          </div>
        </header>

        {/* Sandbox view */}
        {activeTab === "sandbox" && (
          <div className="flex-1 flex overflow-hidden">
            {/* Chat Frame */}
            <div className="flex-1 flex flex-col min-w-0 border-r border-[#1F293D]">
              {/* Messages feed */}
              <div className="flex-1 overflow-y-auto p-6 space-y-6">
                {messages.length === 0 && !streamingResponse && !streamingThought && !toolCalls.length ? (
                  <div className="h-full flex flex-col items-center justify-center text-slate-500 text-center max-w-md mx-auto">
                    <div className="h-12 w-12 rounded-full bg-slate-800/80 border border-slate-700 flex items-center justify-center text-slate-400 text-lg mb-4">
                      💬
                    </div>
                    <h3 className="text-sm font-bold text-slate-300">Start conversations with the Sales SDR</h3>
                    <p className="text-xs text-slate-500 mt-2">
                      Send a message as a prospect to test qualification, Twilio WhatsApp human overrides, or secure POS read-only product and order database querying.
                    </p>
                  </div>
                ) : (
                  <>
                    {/* Render past conversation */}
                    {messages.map((msg, idx) => (
                      <div
                        key={idx}
                        className={`flex gap-4 max-w-4xl ${
                          msg.role === "user" ? "ml-auto flex-row-reverse" : ""
                        }`}
                      >
                        {/* Avatar */}
                        <div className={`h-8 w-8 rounded-lg flex items-center justify-center shrink-0 text-xs font-extrabold ${
                          msg.role === "user" 
                            ? "bg-slate-700 text-white" 
                            : "bg-gradient-to-tr from-sky-500 to-indigo-600 text-white shadow-md shadow-indigo-500/10"
                        }`}>
                          {msg.role === "user" ? "U" : "SDR"}
                        </div>

                        {/* Speech Bubble */}
                        <div className="space-y-2 max-w-[85%]">
                          <div className={`rounded-xl px-4 py-2.5 text-sm leading-relaxed border ${
                            msg.role === "user"
                              ? "bg-[#1E293B] text-slate-100 border-[#2D3D54] shadow-sm shadow-slate-900/5"
                              : "bg-[#161F30]/80 text-slate-200 border-[#202E47]"
                          }`}>
                            {msg.content}
                          </div>

                          {/* Show thoughts if present */}
                          {msg.thought && (
                            <details className="group bg-[#111624] border border-slate-800/80 rounded-lg overflow-hidden transition-all duration-200">
                              <summary className="cursor-pointer select-none text-[10px] font-bold text-indigo-400 uppercase tracking-wider px-3 py-1.5 hover:bg-slate-800/50 flex items-center gap-1.5">
                                <span className="text-xs group-open:rotate-90 transition-transform duration-200">▶</span>
                                💡 SDR Thought Process
                              </summary>
                              <div className="px-4 pb-3 pt-1 text-xs text-slate-400 font-mono leading-relaxed whitespace-pre-wrap border-t border-slate-800/50 bg-[#0F1320]">
                                {msg.thought}
                              </div>
                            </details>
                          )}
                        </div>
                      </div>
                    ))}

                    {/* Render running/completed tools */}
                    {toolCalls.map((call, idx) => (
                      <div key={`tool-${idx}`} className="flex gap-4 max-w-4xl">
                        <div className="h-8 w-8 rounded-lg bg-slate-800 text-slate-400 border border-slate-700 flex items-center justify-center shrink-0 font-mono text-[9px] font-black uppercase">
                          Tool
                        </div>
                        <div className="flex-1 bg-[#101726]/60 border border-slate-800/80 rounded-lg p-3 text-xs font-mono max-w-[85%]">
                          <div className="flex items-center justify-between text-slate-400 border-b border-slate-800/80 pb-1.5 mb-1.5">
                            <span className="font-bold text-sky-400">🔧 tool_call: {call.tool}()</span>
                            <span className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold ${
                              call.status === "running" ? "bg-indigo-950 text-indigo-300 animate-pulse" : "bg-emerald-950 text-emerald-300"
                            }`}>
                              {call.status}
                            </span>
                          </div>
                          <div className="text-[10px] text-slate-400 mt-1">
                            <span className="font-bold text-slate-300">Inputs:</span> {JSON.stringify(call.inputs, null, 2)}
                          </div>
                          {call.output && (
                            <div className="text-[10px] text-slate-300 border-t border-slate-800/50 pt-1.5 mt-1.5 max-h-48 overflow-y-auto whitespace-pre-wrap">
                              <span className="font-bold text-emerald-400">Output:</span> {call.output}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}

                    {/* Render active stream states */}
                    {(streamingThought || streamingResponse) && (
                      <div className="flex gap-4 max-w-4xl">
                        <div className="h-8 w-8 rounded-lg bg-gradient-to-tr from-sky-500 to-indigo-600 text-white flex items-center justify-center shrink-0 text-xs font-extrabold animate-pulse">
                          SDR
                        </div>
                        <div className="space-y-3 flex-1 max-w-[85%]">
                          {/* Live Streaming Thought */}
                          {streamingThought && (
                            <div className="bg-[#111624] border border-indigo-950/40 rounded-lg overflow-hidden">
                              <div className="text-[10px] font-bold text-indigo-400 uppercase tracking-wider px-3 py-1.5 border-b border-slate-800/50 bg-[#0F1320] flex items-center gap-1.5">
                                <span className="h-1.5 w-1.5 rounded-full bg-indigo-400 animate-ping" />
                                💡 SDR Thought Process (Reasoning...)
                              </div>
                              <div className="px-4 py-2.5 text-xs text-indigo-300/80 font-mono leading-relaxed whitespace-pre-wrap bg-[#0F1320]">
                                {streamingThought}
                              </div>
                            </div>
                          )}

                          {/* Live Streaming Response text */}
                          {streamingResponse && (
                            <div className="rounded-xl px-4 py-2.5 text-sm leading-relaxed border bg-[#161F30]/80 text-slate-200 border-[#202E47]">
                              {streamingResponse}
                              <span className="inline-block w-1.5 h-3.5 bg-sky-400 animate-pulse ml-0.5" />
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </>
                )}
                <div ref={chatEndRef} />
              </div>

              {/* Chat Input block */}
              <div className="p-4 border-t border-[#1F293D] bg-[#111726]/40 backdrop-blur-md">
                {activeLead?.status === "Handoff Requested" || activeLead?.status === "Human Claimed" ? (
                  <div className="bg-amber-950/20 border border-amber-800/30 rounded-lg p-3 flex items-center justify-between">
                    <span className="text-xs text-amber-400 font-semibold">
                      ⚠️ Chat is in Human Intervention Mode (Twilio Handoff Triggered). Use the **Supervisor Console** to respond.
                    </span>
                    <button
                      onClick={() => setActiveTab("supervisor")}
                      className="px-3 py-1 bg-amber-600 hover:bg-amber-500 text-black text-xs font-bold rounded-lg transition-all"
                    >
                      Go to Supervisor
                    </button>
                  </div>
                ) : (
                  <div className="flex gap-3">
                    <input
                      type="text"
                      value={chatInput}
                      onChange={(e) => setChatInput(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && handleSendMessage()}
                      placeholder="Type a message as a lead..."
                      className="flex-1 bg-[#1A2234] border border-[#2D3D54] rounded-lg px-4 py-2.5 text-sm text-[#F1F5F9] placeholder-slate-500 focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 transition-all"
                    />
                    <button
                      onClick={handleSendMessage}
                      disabled={!chatInput.trim() || !connected}
                      className="px-5 py-2.5 bg-gradient-to-r from-sky-500 to-indigo-600 hover:from-sky-400 hover:to-indigo-500 disabled:opacity-40 disabled:pointer-events-none text-white text-sm font-semibold rounded-lg shadow-md shadow-indigo-600/10 transition-all flex items-center gap-1.5"
                    >
                      Send
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3" />
                      </svg>
                    </button>
                  </div>
                )}
              </div>
            </div>

            {/* Sandbox Side Panel - Real-time Lead profile details */}
            <div className="w-80 overflow-y-auto p-6 space-y-6">
              <div>
                <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Enriched Lead Profile</h3>
                {activeLead ? (
                  <div className="bg-[#161F30]/40 border border-[#1F293D] rounded-xl p-4 space-y-4">
                    <div>
                      <div className="text-[10px] text-slate-400 font-bold uppercase">Company</div>
                      <div className="text-sm font-bold text-white truncate">{activeLead.company || "Pending Verification"}</div>
                    </div>
                    <div>
                      <div className="text-[10px] text-slate-400 font-bold uppercase">Job Title</div>
                      <div className="text-sm font-bold text-white truncate">{activeLead.job_title || "Pending Verification"}</div>
                    </div>
                    <div>
                      <div className="text-[10px] text-slate-400 font-bold uppercase">Qualification status</div>
                      <div className="mt-1">{renderStatusBadge(activeLead.status)}</div>
                    </div>
                    <div>
                      <div className="text-[10px] text-slate-400 font-bold uppercase">Intent Score</div>
                      <div className="flex items-center gap-2 mt-1">
                        <div className="flex-1 bg-slate-800 rounded-full h-1.5 overflow-hidden">
                          <div
                            className="bg-indigo-500 h-full rounded-full transition-all duration-300"
                            style={{ width: `${(activeLead.intent_score || 0) * 10}%` }}
                          />
                        </div>
                        <span className="text-xs font-bold text-slate-300">{activeLead.intent_score || 0}/10</span>
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] text-slate-400 font-bold uppercase">B2B Firmographic Fit</div>
                      <div className="mt-1">
                        {activeLead.fit === true ? (
                          <span className="inline-flex items-center gap-1 text-xs text-emerald-400 font-bold">
                            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> Matches Profile
                          </span>
                        ) : activeLead.fit === false ? (
                          <span className="inline-flex items-center gap-1 text-xs text-rose-400 font-bold">
                            <span className="h-1.5 w-1.5 rounded-full bg-rose-400" /> Out of Profile
                          </span>
                        ) : (
                          <span className="text-xs text-slate-500 font-semibold">Not evaluated yet</span>
                        )}
                      </div>
                    </div>
                    {activeLead.status === "Handoff Requested" && (
                      <div className="bg-amber-950/20 border border-amber-900/30 p-2.5 rounded-lg text-xs">
                        <div className="font-bold text-amber-400 mb-0.5">Handoff reason:</div>
                        <div className="text-slate-300 italic">"{activeLead.handoff_reason}"</div>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="bg-[#161F30]/20 border border-dashed border-[#1F293D] rounded-xl p-6 text-center text-xs text-slate-500">
                    No lead profile stored for this thread. Engage in chat to verify firmographics.
                  </div>
                )}
              </div>

              {/* CRM Stub Data description */}
              <div>
                <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Simulated POS & twilio</h3>
                <p className="text-[11px] text-slate-500 leading-relaxed">
                  Ask about stock (e.g. *"Do you have professional packages?"*) or look up order status (e.g. *"Check order status for 1001 with email cto@cloudgrid.io"*). Handoffs push live alerts via Twilio's WhatsApp gateway.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* Handoff Supervisor view */}
        {activeTab === "supervisor" && (
          <div className="flex-1 flex overflow-hidden">
            {/* Left Queue */}
            <div className="w-80 border-r border-[#1F293D] flex flex-col bg-[#111726]/20">
              <div className="p-4 border-b border-[#1F293D] text-[11px] font-bold text-slate-400 uppercase tracking-wider">
                Handoff Inboxes ({leads.filter((l) => ["Handoff Requested", "Human Claimed"].includes(l.status || "")).length})
              </div>
              <div className="flex-1 overflow-y-auto p-4 space-y-2">
                {leads.filter((l) => ["Handoff Requested", "Human Claimed"].includes(l.status || "")).length === 0 ? (
                  <div className="text-center text-xs text-slate-500 py-8">
                    Queue clear. No active handoff requests!
                  </div>
                ) : (
                  leads
                    .filter((l) => ["Handoff Requested", "Human Claimed"].includes(l.status || ""))
                    .map((lead) => (
                      <button
                        key={lead.thread_id}
                        onClick={() => {
                          setSelectedHandoffThread(lead.thread_id);
                          setThreadId(lead.thread_id);
                        }}
                        className={`w-full text-left p-3.5 rounded-xl border transition-all ${
                          selectedHandoffThread === lead.thread_id
                            ? "bg-[#1E293B] text-white border-sky-500/30"
                            : "bg-[#161F30]/40 text-slate-300 hover:bg-[#1E293B]/40 border-[#1F293D]"
                        }`}
                      >
                        <div className="flex items-center justify-between mb-1.5">
                          <span className="font-bold text-xs truncate max-w-[120px]">
                            {lead.company || "Verifying Company"}
                          </span>
                          {renderStatusBadge(lead.status)}
                        </div>
                        <div className="text-[10px] text-slate-400 truncate mb-1">
                          Reason: {lead.handoff_reason || "None stated"}
                        </div>
                        <div className="text-[9px] font-mono text-slate-500">
                          {lead.thread_id}
                        </div>
                      </button>
                    ))
                )}
              </div>
            </div>

            {/* Right Operator Console */}
            <div className="flex-1 flex flex-col bg-[#0C1220] min-w-0">
              {selectedHandoffThread ? (
                <>
                  {/* Console Header */}
                  <div className="p-4 border-b border-[#1F293D] flex items-center justify-between bg-[#111726]/40 backdrop-blur-md shrink-0">
                    <div>
                      <h4 className="text-xs font-bold text-slate-400 uppercase">Selected Thread</h4>
                      <h3 className="text-sm font-bold text-white font-mono">{selectedHandoffThread}</h3>
                    </div>
                    {leads.find((l) => l.thread_id === selectedHandoffThread)?.status === "Handoff Requested" ? (
                      <button
                        onClick={() => handleClaimThread(selectedHandoffThread)}
                        className="py-1.5 px-4 bg-amber-500 hover:bg-amber-400 text-black text-xs font-bold rounded-lg transition-all"
                      >
                        Claim Handoff
                      </button>
                    ) : (
                      <button
                        onClick={() => handleResolveThread(selectedHandoffThread)}
                        className="py-1.5 px-4 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold rounded-lg transition-all"
                      >
                        Return to SDR Bot
                      </button>
                    )}
                  </div>

                  {/* Active Transcript view */}
                  <div className="flex-1 overflow-y-auto p-6 space-y-4">
                    {messages.map((msg, idx) => (
                      <div
                        key={idx}
                        className={`flex gap-4 max-w-4xl ${
                          msg.role === "user" ? "ml-auto flex-row-reverse" : ""
                        }`}
                      >
                        <div className={`h-8 w-8 rounded-lg flex items-center justify-center shrink-0 text-xs font-extrabold ${
                          msg.role === "user" ? "bg-slate-700 text-white" : "bg-[#1E293B] text-sky-400"
                        }`}>
                          {msg.role === "user" ? "U" : "OP"}
                        </div>
                        <div className="space-y-1 max-w-[85%]">
                          <div className={`rounded-xl px-4 py-2.5 text-sm leading-relaxed border ${
                            msg.role === "user"
                              ? "bg-[#1E293B] text-slate-100 border-[#2D3D54]"
                              : "bg-[#161F30]/80 text-slate-200 border-[#202E47]"
                          }`}>
                            {msg.content}
                          </div>
                          {msg.thought && (
                            <div className="text-[10px] font-mono text-slate-500 px-2 italic">
                              ({msg.thought})
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                    <div ref={supervisorChatEndRef} />
                  </div>

                  {/* Operator send message interface */}
                  <div className="p-4 border-t border-[#1F293D] bg-[#111726]/40 backdrop-blur-md shrink-0">
                    {leads.find((l) => l.thread_id === selectedHandoffThread)?.status === "Human Claimed" ? (
                      <div className="flex gap-3">
                        <input
                          type="text"
                          value={supervisorMessage}
                          onChange={(e) => setSupervisorMessage(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && handleSendSupervisorMessage(selectedHandoffThread)}
                          placeholder="Reply as human representative..."
                          className="flex-1 bg-[#1A2234] border border-[#2D3D54] rounded-lg px-4 py-2.5 text-sm text-[#F1F5F9] focus:outline-none focus:border-purple-500 transition-all"
                        />
                        <button
                          onClick={() => handleSendSupervisorMessage(selectedHandoffThread)}
                          disabled={!supervisorMessage.trim()}
                          className="px-5 py-2.5 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 disabled:opacity-40 disabled:pointer-events-none text-white text-sm font-semibold rounded-lg shadow-md transition-all"
                        >
                          Send Operator Message
                        </button>
                      </div>
                    ) : (
                      <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-4 text-center text-xs text-slate-500 font-semibold">
                        💡 Claim this conversation queue to take over typing control and communicate with client.
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <div className="flex-1 flex flex-col items-center justify-center text-slate-500 p-6 text-center">
                  <div className="h-12 w-12 rounded-full border border-slate-800 flex items-center justify-center text-slate-400 text-lg mb-4">
                    📋
                  </div>
                  <h3 className="text-sm font-bold text-slate-300">Select Handoff Thread</h3>
                  <p className="text-xs text-slate-500 max-w-sm mt-2">
                    Verify threads flagged by the agent for intervention. Take over control where leads ask for humans or display immediate buying patterns.
                  </p>
                </div>
              )}
            </div>
          </div>
        )}

        {/* CRM leads view */}
        {activeTab === "leads" && (
          <div className="flex-1 overflow-y-auto p-8 space-y-6">
            <div>
              <h2 className="text-lg font-bold text-white mb-1">B2B Synced Leads</h2>
              <p className="text-xs text-slate-400">
                Leads automatically sync'd to MongoDB Atlas from LangGraph SDR qualifying tools.
              </p>
            </div>

            <div className="bg-[#111726]/40 border border-[#1F293D] rounded-xl overflow-hidden shadow-xl">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-[#1F293D] bg-[#161F30]/40 text-slate-400 text-[10px] font-bold uppercase tracking-wider">
                    <th className="py-4 px-6">Company</th>
                    <th className="py-4 px-6">Job Title</th>
                    <th className="py-4 px-6">Intent Score</th>
                    <th className="py-4 px-6">B2B Fit</th>
                    <th className="py-4 px-6">Status</th>
                    <th className="py-4 px-6">Thread ID</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1F293D] text-sm text-slate-200">
                  {leads.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="text-center py-12 text-xs text-slate-500 font-semibold">
                        No leads sync'd in database. Complete firmographic checks to qualify leads.
                      </td>
                    </tr>
                  ) : (
                    leads.map((lead, idx) => (
                      <tr key={idx} className="hover:bg-[#1E293B]/20 transition-all">
                        <td className="py-4 px-6 font-bold text-white">{lead.company || "N/A"}</td>
                        <td className="py-4 px-6">{lead.job_title || "N/A"}</td>
                        <td className="py-4 px-6">
                          <span className="font-mono font-bold text-slate-300">{lead.intent_score || 0}/10</span>
                        </td>
                        <td className="py-4 px-6">
                          {lead.fit === true ? (
                            <span className="text-emerald-400 font-bold text-xs flex items-center gap-1">
                              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> Yes
                            </span>
                          ) : lead.fit === false ? (
                            <span className="text-rose-400 font-bold text-xs flex items-center gap-1">
                              <span className="h-1.5 w-1.5 rounded-full bg-rose-400" /> No
                            </span>
                          ) : (
                            <span className="text-slate-500 font-semibold text-xs">Unknown</span>
                          )}
                        </td>
                        <td className="py-4 px-6">{renderStatusBadge(lead.status)}</td>
                        <td className="py-4 px-6 font-mono text-xs text-slate-400">{lead.thread_id}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
