"use client";

import React, { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import Vapi from "@vapi-ai/web";
import AdminIntegrations from "../components/AdminIntegrations";
import {
  authHeaders,
  clearSession,
  fetchMe,
  getAccessToken,
  getBackendUrl,
  getStoredApiKey,
  getStoredUser,
  saveApiKey,
  type AuthUser,
} from "@/lib/auth";

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
  const router = useRouter();
  const [authChecked, setAuthChecked] = useState(false);
  const [sessionUser, setSessionUser] = useState<AuthUser | null>(null);
  const [regeneratedKey, setRegeneratedKey] = useState<string | null>(null);
  const [regeneratingKey, setRegeneratingKey] = useState(false);
  const [threadId, setThreadId] = useState<string>("");
  const [threads, setThreads] = useState<Array<{ thread_id: string, title?: string }>>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [activeLead, setActiveLead] = useState<Lead | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [activeTab, setActiveTab] = useState<"sandbox" | "supervisor" | "leads" | "appointments" | "orders" | "admin">("sandbox");
  const [appointments, setAppointments] = useState<any[]>([]);
  const [orders, setOrders] = useState<any[]>([]);

  // Real-time states
  const [connected, setConnected] = useState<boolean>(false);
  const [statusText, setStatusText] = useState<string>("Disconnected");
  const [streamingThought, setStreamingThought] = useState<string>("");
  const [streamingResponse, setStreamingResponse] = useState<string>("");
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);

  const [apiKey, setApiKey] = useState<string>("");
  const [tenantInfo, setTenantInfo] = useState<{ tenant_id: string; org_name: string } | null>(null);
  const [backendUrl, setBackendUrl] = useState<string>(() =>
    typeof window !== "undefined" ? getBackendUrl() : "http://127.0.0.1:8765"
  );
  const [voiceEnabled, setVoiceEnabled] = useState<boolean>(false);
  const voiceEnabledRef = useRef(false);
  const [isCalling, setIsCalling] = useState<boolean>(false);
  const [activeVoiceCallId, setActiveVoiceCallId] = useState<string | null>(null);
  const vapiRef = useRef<any>(null);
  const activeVapiCallIdRef = useRef<string | null>(null);
  const isCallingRef = useRef(false);
  const threadIdRef = useRef(threadId);
  const accessTokenRef = useRef<string | null>(null);
  const apiKeyRef = useRef(apiKey);
  const backendUrlRef = useRef(backendUrl);
  const tenantIdRef = useRef("alpha_default");
  const orgNameRef = useRef("");

  useEffect(() => { threadIdRef.current = threadId; }, [threadId]);
  useEffect(() => { apiKeyRef.current = apiKey; }, [apiKey]);
  useEffect(() => { backendUrlRef.current = backendUrl; }, [backendUrl]);
  useEffect(() => { isCallingRef.current = isCalling; }, [isCalling]);
  useEffect(() => { accessTokenRef.current = getAccessToken(); }, [authChecked]);

  const orgDisplayName = tenantInfo?.org_name || sessionUser?.org_name || "Console";
  const orgInitial = orgDisplayName.trim().charAt(0).toUpperCase() || "C";

  useEffect(() => {
    if (tenantInfo?.org_name) {
      document.title = `${tenantInfo.org_name} — Sales Agent`;
    }
  }, [tenantInfo?.org_name]);

  useEffect(() => {
    const initAuth = async () => {
      const token = getAccessToken();
      if (!token) {
        router.replace("/login");
        return;
      }
      const stored = getStoredUser();
      if (stored?.role === "super_admin") {
        router.replace("/super-admin");
        return;
      }
      const me = await fetchMe(backendUrl);
      if (!me || !me.tenant_id) {
        clearSession();
        router.replace("/login");
        return;
      }
      setSessionUser(me);
      setTenantInfo({ tenant_id: me.tenant_id, org_name: me.org_name || me.tenant_id });
      tenantIdRef.current = me.tenant_id;
      orgNameRef.current = me.org_name || "";
      const savedKey = getStoredApiKey();
      if (savedKey) setApiKey(savedKey);
      setAuthChecked(true);
    };
    initAuth();
  }, [router, backendUrl]);

  useEffect(() => {
    if (!authChecked) return;
    const fetchTenantId = async () => {
      try {
        const res = await fetch(`${backendUrl}/api/voice/public-key`, {
          headers: getHeaders(),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.tenant_id) tenantIdRef.current = data.tenant_id;
        }
      } catch (e) {
        console.error("Failed to fetch tenant id:", e);
      }
    };
    fetchTenantId();
  }, [authChecked, backendUrl]);

  const linkCallToThread = async (callId: string) => {
    if (!callId || !threadIdRef.current) return;
    activeVapiCallIdRef.current = callId;
    setActiveVoiceCallId(callId);
    try {
      await fetch(`${backendUrlRef.current}/api/voice/link`, {
        method: "POST",
        headers: { ...getHeaders() },
        body: JSON.stringify({ call_id: callId, console_thread_id: threadIdRef.current }),
      });
    } catch (e) {
      console.error("Failed to link voice call to chat thread:", e);
    }
  };

  // Inputs
  const [chatInput, setChatInput] = useState<string>("");
  const [supervisorMessage, setSupervisorMessage] = useState<string>("");
  const [selectedHandoffThread, setSelectedHandoffThread] = useState<string>("");
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(false);
  const [showLeadProfile, setShowLeadProfile] = useState<boolean>(false);
  const [supervisorMobileView, setSupervisorMobileView] = useState<"queue" | "chat">("queue");

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
  const getHeaders = () => authHeaders();

  const getWsAuthQuery = () => {
    const token = getAccessToken();
    if (token) return `token=${encodeURIComponent(token)}`;
    if (apiKey) return `api_key=${encodeURIComponent(apiKey)}`;
    return "";
  };

  const handleLogout = () => {
    clearSession();
    router.push("/login");
  };

  const handleRegenerateApiKey = async () => {
    if (!confirm("This will invalidate your current API key. Continue?")) return;
    setRegeneratingKey(true);
    try {
      const res = await fetch(`${backendUrl}/api/auth/regenerate-api-key`, {
        method: "POST",
        headers: getHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to regenerate");
      saveApiKey(data.api_key);
      setApiKey(data.api_key);
      setRegeneratedKey(data.api_key);
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : "Failed to regenerate API key");
    } finally {
      setRegeneratingKey(false);
    }
  };

  const handleBackendUrlChange = (val: string) => {
    setBackendUrl(val);
    if (typeof window !== "undefined") {
      localStorage.setItem("sdr_backend_url", val);
    }
  };

  const getWsUrl = (url: string) => {
    try {
      const u = new URL(url);
      const protocol = u.protocol === "https:" ? "wss:" : "ws:";
      return `${protocol}//${u.host}`;
    } catch (e) {
      return "wss://salesagent-b6po.onrender.com";
    }
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

  useEffect(() => {
    if (typeof window !== "undefined") {
      const vapiPublicKey = process.env.NEXT_PUBLIC_VAPI_PUBLIC_KEY || "e8cd4840-5e48-4005-9b73-353ac169e70e";
      vapiRef.current = new Vapi(vapiPublicKey);

      vapiRef.current.on("call-start", async (call: { id?: string }) => {
        isCallingRef.current = true;
        setIsCalling(true);
        setStatusText("Vapi call connected!");
        const callId = call?.id;
        if (callId) {
          await linkCallToThread(callId);
        }
      });

      vapiRef.current.on("call-end", async () => {
        isCallingRef.current = false;
        setIsCalling(false);
        setStatusText("Vapi call ended.");
        const callId = activeVapiCallIdRef.current;
        if (callId) {
          try {
            await fetch(`${backendUrlRef.current}/api/voice/link/${callId}`, {
              method: "DELETE",
              headers: getHeaders(),
            });
          } catch (e) {
            console.error("Failed to unlink voice call:", e);
          }
          activeVapiCallIdRef.current = null;
          setActiveVoiceCallId(null);
        }
      });

      vapiRef.current.on("message", (message: { type?: string; call?: { id?: string } }) => {
        const callId = message?.call?.id;
        if (callId && !activeVapiCallIdRef.current) {
          linkCallToThread(callId);
        }
      });

      vapiRef.current.on("error", (e: any) => {
        console.error("Vapi call error:", e);
        isCallingRef.current = false;
        setIsCalling(false);
        setStatusText("Vapi error: " + (e.message || "Failed"));
      });
    }

    return () => {
      if (vapiRef.current) {
        vapiRef.current.stop();
      }
    };
  }, []);

  const handleToggleVapiCall = async () => {
    if (isCalling) {
      vapiRef.current.stop();
    } else {
      setStatusText("Connecting Vapi Call...");
      try {
        if (threadIdRef.current) {
          await fetch(`${backendUrlRef.current}/api/voice/register-session`, {
            method: "POST",
            headers: { ...getHeaders() },
            body: JSON.stringify({ console_thread_id: threadIdRef.current }),
          });
        }
        const assistantId = process.env.NEXT_PUBLIC_VAPI_ASSISTANT_ID || "a5b5e387-3e26-4ad0-ad0f-8454b675f1c9";
        vapiRef.current.start(assistantId, {
          metadata: {
            console_thread_id: threadIdRef.current,
            tenant_id: tenantIdRef.current,
            org_name: orgNameRef.current,
          },
        });
      } catch (err: any) {
        console.error("Failed to start Vapi call:", err);
        setStatusText("Vapi Error: " + err.message);
      }
    }
  };

  // Initialize a random thread if none exists
  useEffect(() => {
    if (!authChecked) return;
    fetchThreads();
    fetchLeads();

    const randomId = "thread_" + Math.random().toString(36).substring(2, 10);
    setThreadId(randomId);
  }, [authChecked, backendUrl]);

  // Fetch threads and leads lists
  const fetchThreads = async () => {
    try {
      const res = await fetch(`${backendUrl}/api/conversations`, { headers: getHeaders() });
      if (res.ok) {
        const data = await res.json();
        const uniqueThreads: any[] = [];
        const seenIds = new Set();
        for (const t of data) {
          if (t && t.thread_id && !seenIds.has(t.thread_id)) {
            seenIds.add(t.thread_id);
            uniqueThreads.push(t);
          }
        }
        setThreads(uniqueThreads);
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
      const res = await fetch(`${backendUrl}/api/conversations/${id}/title`, {
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
      const res = await fetch(`${backendUrl}/api/conversations/${id}`, {
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
      const res = await fetch(`${backendUrl}/api/leads`, { headers: getHeaders() });
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
      const res = await fetch(`${backendUrl}/api/leads/${id}`, { headers: getHeaders() });
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
    if (!threadId || !authChecked || !tenantInfo) return;

    const wsAuth = getWsAuthQuery();
    if (!wsAuth) return;

    setMessages([]);
    setStreamingThought("");
    setStreamingResponse("");
    setToolCalls([]);
    streamingThoughtRef.current = "";
    streamingResponseRef.current = "";
    toolCallsRef.current = [];

    const socket = new WebSocket(`${getWsUrl(backendUrl)}/ws/chat/${threadId}?${wsAuth}`);

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
      } else if (data.type === "stream_start") {
        setStatusText("Streaming...");
        setStreamingThought("");
        setStreamingResponse("");
        streamingThoughtRef.current = "";
        streamingResponseRef.current = "";
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

    setThreads((prev) => {
      const exists = prev.some((t) => t.thread_id === threadId);
      if (exists) return prev;
      return [...prev, { thread_id: threadId }];
    });

    return () => {
      socket.close();
    };
  }, [threadId, authChecked, backendUrl, tenantInfo, apiKey]);

  // Send message as Sandbox user (or typed-only during active voice call)
  const handleSendMessage = async () => {
    if (!chatInput.trim()) return;

    const userMsg = chatInput.trim();
    const duringCall = isCallingRef.current || !!activeVapiCallIdRef.current;

    // During voice call: save typed detail and inject into Vapi — do not run chat WebSocket agent
    if (duringCall) {
      setMessages((prev) => [...prev, { role: "user", content: userMsg }]);
      setChatInput("");
      try {
        await fetch(`${backendUrl}/api/conversations/${threadId}/typed`, {
          method: "POST",
          headers: getHeaders(),
          body: JSON.stringify({ message: userMsg }),
        });

        // Push typed text into the live Vapi conversation so the voice agent responds immediately
        if (vapiRef.current?.send) {
          vapiRef.current.send({
            type: "add-message",
            message: { role: "user", content: userMsg },
          });
        }

        setStatusText("Sent to voice agent — it should confirm what you typed shortly.");
      } catch (e) {
        console.error("Failed to save typed message:", e);
        setStatusText("Could not save typed message.");
      }
      return;
    }

    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return;

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
    setSidebarOpen(false);
  };

  // Supervisor actions
  const handleClaimThread = async (id: string) => {
    try {
      const res = await fetch(`${backendUrl}/api/handoffs/${id}/claim`, { method: "POST", headers: getHeaders() });
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
      const res = await fetch(`${backendUrl}/api/handoffs/${id}/resolve`, { method: "POST", headers: getHeaders() });
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
      const res = await fetch(`${backendUrl}/api/handoffs/${id}/message`, {
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
    else if (s === "Order Placed") bg = "bg-green-950/60 text-green-400 border-green-800/80";

    return (
      <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold border ${bg}`}>
        {pulse && <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-ping" />}
        {s}
      </span>
    );
  };

  const voiceActive = isCalling || !!activeVoiceCallId;

  if (!authChecked) {
    return (
      <div className="flex h-[100dvh] items-center justify-center bg-[#0A0E1A] text-slate-400">
        Loading console…
      </div>
    );
  }

  return (
    <div className="flex h-[100dvh] overflow-hidden bg-[#0A0E1A] text-[#E2E8F0]">
      {/* Mobile sidebar backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 lg:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Sidebar */}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-[min(20rem,85vw)] shrink-0 flex-col border-r border-[#1F293D] bg-[#111726] transition-transform duration-300 ease-in-out lg:relative lg:w-80 lg:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0"
        }`}
      >
        {/* Branding */}
        <div className="flex items-center justify-between border-b border-[#1F293D] p-4 sm:p-6">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-tr from-sky-500 to-indigo-600 text-sm font-bold text-white shadow-md shadow-indigo-500/20">
              {orgInitial}
            </div>
            <div className="min-w-0">
              <h1 className="text-sm font-extrabold tracking-wide text-white truncate max-w-[11rem] sm:max-w-none">
                {orgDisplayName.toUpperCase()}
              </h1>
              <span className="text-[10px] font-medium text-slate-400">B2B SDR AGENT CONSOLE</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {/* Glowing Status Dot */}
            <div className="flex items-center gap-1.5 rounded-full border border-slate-700 bg-[#1A2333] px-2.5 py-1">
              <span className={`h-2.5 w-2.5 rounded-full ${connected ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.5)]" : "bg-rose-500"}`} />
              <span className="text-[10px] font-bold uppercase tracking-wider text-slate-300">
                {connected ? "LIVE" : "OFF"}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setSidebarOpen(false)}
              className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-800 hover:text-white lg:hidden"
              aria-label="Close sidebar"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        {/* Account Panel */}
        <div className="p-4 border-b border-[#1F293D] bg-[#161F30]/20">
          <div className="flex items-center justify-between mb-2">
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
              Signed in
            </label>
            <button
              type="button"
              onClick={handleLogout}
              className="text-[10px] font-bold text-slate-400 hover:text-rose-400 uppercase"
            >
              Sign out
            </button>
          </div>
          {sessionUser && (
            <div className="text-xs text-slate-300 mb-2">
              <p className="font-semibold text-white truncate">{sessionUser.name || sessionUser.email}</p>
              <p className="text-[10px] text-slate-500 truncate">{sessionUser.email}</p>
            </div>
          )}
          {tenantInfo && (
            <p className="text-[10px] text-emerald-400 font-mono mb-3">
              ✓ {tenantInfo.org_name} ({tenantInfo.tenant_id})
            </p>
          )}
          <button
            type="button"
            onClick={handleRegenerateApiKey}
            disabled={regeneratingKey}
            className="w-full py-1.5 text-[10px] font-bold uppercase tracking-wider rounded-lg border border-amber-600/40 text-amber-400 hover:bg-amber-950/30 disabled:opacity-50"
          >
            {regeneratingKey ? "Generating…" : "Regenerate API key"}
          </button>
          {regeneratedKey && (
            <div className="mt-2 rounded-lg bg-[#0A0E1A] border border-emerald-500/30 p-2">
              <p className="text-[9px] text-emerald-400 mb-1 font-bold uppercase">New API key — copy now</p>
              <p className="font-mono text-[10px] text-sky-300 break-all">{regeneratedKey}</p>
            </div>
          )}
          {!apiKey && !regeneratedKey && (
            <p className="text-[10px] text-slate-500 mt-2">
              No API key on this device. Regenerate for voice agents.
            </p>
          )}
          <div className="flex items-center justify-between mt-3 mb-1.5">
            <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
              Backend Service URL
            </label>
            <span className="text-[9px] font-bold text-sky-400 uppercase">Host</span>
          </div>
          <input
            type="text"
            value={backendUrl}
            onChange={(e) => handleBackendUrlChange(e.target.value)}
            placeholder="e.g. https://salesagent-b6po.onrender.com"
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
                  className={`group w-full flex items-center justify-between rounded-lg text-xs font-semibold transition-all border ${threadId === id
                      ? "bg-[#1E293B] text-sky-400 border-sky-500/30 shadow-[0_4px_12px_rgba(0,0,0,0.1)]"
                      : "bg-[#161F30]/40 text-slate-300 hover:bg-[#1E293B]/60 border-transparent"
                    }`}
                >
                  <button
                    onClick={() => {
                      setThreadId(id);
                      setSidebarOpen(false);
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
                  <div className="hidden max-lg:flex lg:group-hover:flex shrink-0 items-center gap-1.5 pr-2">
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
      <main
        className={`flex min-w-0 flex-1 flex-col overflow-hidden ${
          activeTab === "admin" ? "bg-slate-50" : "bg-[#0C1220]"
        }`}
      >
        {/* Navigation Tabs Header */}
        <header
          className={`flex shrink-0 items-center justify-between gap-3 border-b px-4 h-14 sm:px-6 ${
            activeTab === "admin"
              ? "bg-white border-gray-200"
              : "border-[#1F293D] bg-[#111726]/95 backdrop-blur-md"
          }`}
        >
          <div className="flex min-w-0 flex-1 items-center gap-2 sm:gap-3">
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              className={`shrink-0 rounded-lg border p-2 transition-all lg:hidden ${
                activeTab === "admin"
                  ? "border-gray-300 text-gray-600 hover:bg-gray-100"
                  : "border-slate-700 text-slate-400 hover:bg-[#1E293B] hover:text-white"
              }`}
              aria-label="Open sidebar"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
            <nav className="scrollbar-hide flex items-center gap-1 overflow-x-auto">
            {(
              [
                { id: "sandbox", mobile: "Chat", desktop: "Agent Chat", onClick: () => setActiveTab("sandbox") },
                {
                  id: "supervisor",
                  mobile: "Supervisor",
                  desktop: "Supervisor",
                  onClick: () => {
                    setActiveTab("supervisor");
                    setSelectedHandoffThread(threadId);
                    setSupervisorMobileView("queue");
                  },
                  badge: leads.some((l) => l.status === "Handoff Requested"),
                },
                { id: "leads", mobile: "Leads", desktop: "Leads", onClick: () => { setActiveTab("leads"); fetchLeads(); } },
                { id: "appointments", mobile: "Appts", desktop: "Appointments", onClick: async () => {
                  setActiveTab("appointments");
                  try {
                    const res = await fetch(`${backendUrl}/api/appointments`, { headers: getHeaders() });
                    if (res.ok) setAppointments((await res.json()).appointments || []);
                  } catch { /* ignore */ }
                }},
                { id: "orders", mobile: "Orders", desktop: "Orders", onClick: async () => {
                  setActiveTab("orders");
                  try {
                    const res = await fetch(`${backendUrl}/api/orders`, { headers: getHeaders() });
                    if (res.ok) setOrders((await res.json()).orders || []);
                  } catch { /* ignore */ }
                }},
                { id: "admin", mobile: "Integrations", desktop: "Integrations", onClick: () => setActiveTab("admin") },
              ] as const
            ).map((tab) => {
              const active = activeTab === tab.id;
              const light = activeTab === "admin";
              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={tab.onClick}
                  className={`relative shrink-0 inline-flex h-9 items-center rounded-md px-3 text-xs font-semibold transition-colors sm:px-4 ${
                    active
                      ? light
                        ? "bg-blue-600 text-white shadow-sm"
                        : "bg-[#1E293B] text-sky-400 ring-1 ring-sky-500/30"
                      : light
                        ? "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
                        : "text-slate-400 hover:bg-slate-800/60 hover:text-white"
                  }`}
                >
                  <span className="sm:hidden">{tab.mobile}</span>
                  <span className="hidden sm:inline">{tab.desktop}</span>
                  {"badge" in tab && tab.badge && (
                    <span className="absolute -right-0.5 -top-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-amber-500 text-[8px] font-bold text-white">
                      !
                    </span>
                  )}
                </button>
              );
            })}
          </nav>
          </div>

          {activeTab !== "admin" && (
          <div className="flex shrink-0 items-center gap-2 max-sm:justify-end">
            <button
              type="button"
              onClick={handleToggleVapiCall}
              className={`inline-flex h-9 items-center gap-2 rounded-md border px-3 text-xs font-semibold transition-colors ${
                isCalling
                  ? "border-rose-300 bg-rose-50 text-rose-700"
                  : "border-slate-600 bg-slate-800/80 text-sky-400 hover:bg-slate-700/80"
              }`}
            >
              {isCalling ? "End call" : "Start call"}
            </button>
            <div className="hidden lg:block text-right pl-2 border-l border-slate-700">
              <div className="text-[10px] font-medium uppercase tracking-wide text-slate-500">Status</div>
              <div className="max-w-[160px] truncate text-xs font-medium text-slate-300">{statusText}</div>
            </div>
            <button
              type="button"
              onClick={() => {
                fetchThreads();
                fetchLeads();
                if (threadId) fetchLeadProfile(threadId);
              }}
              className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-slate-600 text-slate-400 hover:bg-slate-800 hover:text-white"
              title="Refresh"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 1121.21 7.89M9 11l3-3m0 0l3 3m-3-3v12" />
              </svg>
            </button>
          </div>
          )}
        </header>

        {/* Sandbox view */}
        {activeTab === "sandbox" && (
          <div className="flex flex-1 flex-col overflow-hidden lg:flex-row">
            {/* Chat Frame */}
            <div className="flex min-w-0 flex-1 flex-col lg:border-r lg:border-[#1F293D]">
              {/* Messages feed */}
              <div className="flex-1 space-y-4 overflow-y-auto p-4 sm:space-y-6 sm:p-6">
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
                        className={`flex gap-4 max-w-4xl ${msg.role === "user" ? "ml-auto flex-row-reverse" : ""
                          }`}
                      >
                        {/* Avatar */}
                        <div className={`h-8 w-8 rounded-lg flex items-center justify-center shrink-0 text-xs font-extrabold ${msg.role === "user"
                            ? "bg-slate-700 text-white"
                            : "bg-gradient-to-tr from-sky-500 to-indigo-600 text-white shadow-md shadow-indigo-500/10"
                          }`}>
                          {msg.role === "user" ? "U" : "SDR"}
                        </div>

                        {/* Speech Bubble */}
                        <div className="space-y-2 max-w-[85%]">
                          <div className={`rounded-xl px-4 py-2.5 text-sm leading-relaxed border ${msg.role === "user"
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
                            <span className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold ${call.status === "running" ? "bg-indigo-950 text-indigo-300 animate-pulse" : "bg-emerald-950 text-emerald-300"
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
              <div className="border-t border-[#1F293D] bg-[#111726]/40 p-3 backdrop-blur-md sm:p-4">
                {(isCalling || activeVoiceCallId) && (
                  <div className="mb-3 rounded-lg border border-sky-500/20 bg-sky-500/10 px-3 py-2 text-xs text-sky-300">
                    📞 <strong>Call active.</strong> Type name, email, or phone here when asked — the voice agent will pick it up and read it back to confirm. Spoken dictation may mishear &quot;1&quot; vs &quot;one&quot;, etc.
                  </div>
                )}
                {activeLead?.status === "Handoff Requested" || activeLead?.status === "Human Claimed" ? (
                  <div className="flex flex-col items-start gap-2 rounded-lg border border-amber-800/30 bg-amber-950/20 p-3 sm:flex-row sm:items-center sm:justify-between">
                    <span className="text-xs font-semibold text-amber-400">
                      ⚠️ Chat is in Human Intervention Mode (Twilio Handoff Triggered). Use the **Supervisor Console** to respond.
                    </span>
                    <button
                      onClick={() => setActiveTab("supervisor")}
                      className="shrink-0 rounded-lg bg-amber-600 px-3 py-1 text-xs font-bold text-black transition-all hover:bg-amber-500"
                    >
                      Go to Supervisor
                    </button>
                  </div>
                ) : (
                  <div className="flex gap-2 sm:gap-3">
                    <input
                      type="text"
                      value={chatInput}
                      onChange={(e) => setChatInput(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && handleSendMessage()}
                      placeholder={
                        voiceActive
                          ? "Type name, email, or phone here when the agent asks..."
                          : "Type a message as a lead..."
                      }
                      className="min-w-0 flex-1 rounded-lg border border-[#2D3D54] bg-[#1A2234] px-3 py-2.5 text-sm text-[#F1F5F9] placeholder-slate-500 transition-all focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 sm:px-4"
                    />
                    <button
                      onClick={handleSendMessage}
                      disabled={!chatInput.trim() || (!voiceActive && !connected)}
                      className="flex shrink-0 items-center gap-1.5 rounded-lg bg-gradient-to-r from-sky-500 to-indigo-600 px-4 py-2.5 text-sm font-semibold text-white shadow-md shadow-indigo-600/10 transition-all hover:from-sky-400 hover:to-indigo-500 disabled:pointer-events-none disabled:opacity-40 sm:px-5"
                    >
                      <span className="hidden sm:inline">Send</span>
                      <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3" />
                      </svg>
                    </button>
                  </div>
                )}
              </div>
            </div>

            {/* Sandbox Side Panel - Real-time Lead profile details */}
            <div className="w-full shrink-0 border-t border-[#1F293D] lg:w-80 lg:border-l lg:border-t-0">
              <button
                type="button"
                onClick={() => setShowLeadProfile((prev) => !prev)}
                className="flex w-full items-center justify-between border-b border-[#1F293D] px-4 py-2.5 text-xs font-bold uppercase tracking-wider text-slate-400 lg:hidden"
              >
                <span>Lead Profile {activeLead?.company ? `· ${activeLead.company}` : ""}</span>
                <svg
                  className={`h-4 w-4 transition-transform ${showLeadProfile ? "rotate-180" : ""}`}
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2}
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              <div className={`max-h-[45vh] space-y-6 overflow-y-auto p-4 sm:p-6 lg:max-h-none ${showLeadProfile ? "block" : "hidden lg:block"}`}>
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
          </div>
        )}

        {/* Handoff Supervisor view */}
        {activeTab === "supervisor" && (
          <div className="flex flex-1 flex-col overflow-hidden lg:flex-row">
            {/* Left Queue */}
            <div className={`flex w-full flex-col border-b border-[#1F293D] bg-[#111726]/20 lg:w-80 lg:border-b-0 lg:border-r ${selectedHandoffThread && supervisorMobileView === "chat" ? "hidden lg:flex" : "flex"}`}>
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
                          setSupervisorMobileView("chat");
                        }}
                        className={`w-full text-left p-3.5 rounded-xl border transition-all ${selectedHandoffThread === lead.thread_id
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
            <div className={`min-w-0 flex-1 flex-col bg-[#0C1220] ${selectedHandoffThread && supervisorMobileView === "chat" ? "flex" : "hidden lg:flex"} ${!selectedHandoffThread ? "lg:flex" : ""}`}>
              {selectedHandoffThread ? (
                <>
                  {/* Console Header */}
                  <div className="flex shrink-0 flex-col gap-3 border-b border-[#1F293D] bg-[#111726]/40 p-4 backdrop-blur-md sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex min-w-0 items-start gap-3">
                      <button
                        type="button"
                        onClick={() => setSupervisorMobileView("queue")}
                        className="mt-0.5 shrink-0 rounded-lg border border-slate-700 p-1.5 text-slate-400 transition-all hover:bg-[#1E293B] hover:text-white lg:hidden"
                        aria-label="Back to queue"
                      >
                        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
                        </svg>
                      </button>
                      <div className="min-w-0">
                        <h4 className="text-xs font-bold uppercase text-slate-400">Selected Thread</h4>
                        <h3 className="truncate text-sm font-bold font-mono text-white">{selectedHandoffThread}</h3>
                      </div>
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
                  <div className="flex-1 space-y-4 overflow-y-auto p-4 sm:p-6">
                    {messages.map((msg, idx) => (
                      <div
                        key={idx}
                        className={`flex gap-4 max-w-4xl ${msg.role === "user" ? "ml-auto flex-row-reverse" : ""
                          }`}
                      >
                        <div className={`h-8 w-8 rounded-lg flex items-center justify-center shrink-0 text-xs font-extrabold ${msg.role === "user" ? "bg-slate-700 text-white" : "bg-[#1E293B] text-sky-400"
                          }`}>
                          {msg.role === "user" ? "U" : "OP"}
                        </div>
                        <div className="space-y-1 max-w-[85%]">
                          <div className={`rounded-xl px-4 py-2.5 text-sm leading-relaxed border ${msg.role === "user"
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
                  <div className="shrink-0 border-t border-[#1F293D] bg-[#111726]/40 p-3 backdrop-blur-md sm:p-4">
                    {leads.find((l) => l.thread_id === selectedHandoffThread)?.status === "Human Claimed" ? (
                      <div className="flex flex-col gap-2 sm:flex-row sm:gap-3">
                        <input
                          type="text"
                          value={supervisorMessage}
                          onChange={(e) => setSupervisorMessage(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && handleSendSupervisorMessage(selectedHandoffThread)}
                          placeholder="Reply as human representative..."
                          className="min-w-0 flex-1 rounded-lg border border-[#2D3D54] bg-[#1A2234] px-4 py-2.5 text-sm text-[#F1F5F9] transition-all focus:border-purple-500 focus:outline-none"
                        />
                        <button
                          onClick={() => handleSendSupervisorMessage(selectedHandoffThread)}
                          disabled={!supervisorMessage.trim()}
                          className="shrink-0 rounded-lg bg-gradient-to-r from-purple-600 to-indigo-600 px-4 py-2.5 text-sm font-semibold text-white shadow-md transition-all hover:from-purple-500 hover:to-indigo-500 disabled:pointer-events-none disabled:opacity-40 sm:px-5"
                        >
                          <span className="hidden sm:inline">Send Operator Message</span>
                          <span className="sm:hidden">Send</span>
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
          <div className="flex-1 space-y-4 overflow-y-auto p-4 sm:space-y-6 sm:p-6 lg:p-8">
            <div>
              <h2 className="mb-1 text-lg font-bold text-white">B2B Synced Leads</h2>
              <p className="text-xs text-slate-400">
                Leads automatically sync'd to MongoDB Atlas from LangGraph SDR qualifying tools.
              </p>
            </div>

            <div className="overflow-x-auto rounded-xl border border-[#1F293D] bg-[#111726]/40 shadow-xl">
              <table className="w-full min-w-[640px] border-collapse text-left">
                <thead>
                  <tr className="border-b border-[#1F293D] bg-[#161F30]/40 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                    <th className="px-4 py-3 sm:px-6 sm:py-4">Company</th>
                    <th className="px-4 py-3 sm:px-6 sm:py-4">Job Title</th>
                    <th className="px-4 py-3 sm:px-6 sm:py-4">Intent Score</th>
                    <th className="px-4 py-3 sm:px-6 sm:py-4">B2B Fit</th>
                    <th className="px-4 py-3 sm:px-6 sm:py-4">Status</th>
                    <th className="px-4 py-3 sm:px-6 sm:py-4">Thread ID</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1F293D] text-sm text-slate-200">
                  {leads.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="py-12 text-center text-xs font-semibold text-slate-500">
                        No leads sync'd in database. Complete firmographic checks to qualify leads.
                      </td>
                    </tr>
                  ) : (
                    leads.map((lead, idx) => (
                      <tr key={idx} className="transition-all hover:bg-[#1E293B]/20">
                        <td className="px-4 py-3 font-bold text-white sm:px-6 sm:py-4">{lead.company || "N/A"}</td>
                        <td className="px-4 py-3 sm:px-6 sm:py-4">{lead.job_title || "N/A"}</td>
                        <td className="px-4 py-3 sm:px-6 sm:py-4">
                          <span className="font-mono font-bold text-slate-300">{lead.intent_score || 0}/10</span>
                        </td>
                        <td className="px-4 py-3 sm:px-6 sm:py-4">
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
                        <td className="px-4 py-3 sm:px-6 sm:py-4">{renderStatusBadge(lead.status)}</td>
                        <td className="max-w-[120px] truncate px-4 py-3 font-mono text-xs text-slate-400 sm:max-w-none sm:px-6 sm:py-4">{lead.thread_id}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ── Appointments Panel ── */}
        {activeTab === "appointments" && (
          <div className="flex h-full flex-col gap-4 p-4 sm:gap-5 sm:p-6">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className="text-lg font-bold tracking-tight text-white">📅 Scheduled Appointments</h2>
                <p className="mt-0.5 text-xs text-slate-500">Booked via voice or chat — stored in MongoDB</p>
              </div>
              <button
                onClick={async () => {
                  try {
                    const res = await fetch(`${backendUrl}/api/appointments`, {
                      headers: getHeaders()
                    });
                    if (res.ok) {
                      const data = await res.json();
                      setAppointments(data.appointments || []);
                    }
                  } catch { }
                }}
                className="px-3 py-1.5 text-xs font-bold bg-sky-500/10 text-sky-400 border border-sky-500/20 rounded-lg hover:bg-sky-500/20 transition-all"
              >
                ↻ Refresh
              </button>
            </div>
            <div className="flex-1 overflow-auto rounded-xl border border-[#1E293B]">
              <table className="w-full min-w-[860px] text-sm text-slate-300">
                <thead>
                  <tr className="border-b border-[#1E293B] bg-[#0F172A] text-xs uppercase tracking-widest text-slate-500">
                    <th className="px-3 py-3 text-left sm:px-5">Name</th>
                    <th className="px-3 py-3 text-left sm:px-5">Email</th>
                    <th className="px-3 py-3 text-left sm:px-5">Phone</th>
                    <th className="px-3 py-3 text-left sm:px-5">Date</th>
                    <th className="px-3 py-3 text-left sm:px-5">Time</th>
                    <th className="px-3 py-3 text-left sm:px-5">Status</th>
                    <th className="px-3 py-3 text-left sm:px-5">Notes</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1E293B]/60">
                  {appointments.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="py-16 text-center text-xs font-semibold text-slate-500">
                        <div className="flex flex-col items-center gap-2 px-4">
                          <span className="text-3xl">📭</span>
                          <span>No appointments yet. Start a Vapi call and ask to book a meeting!</span>
                        </div>
                      </td>
                    </tr>
                  ) : (
                    appointments.map((appt, idx) => (
                      <tr key={idx} className="transition-all hover:bg-[#1E293B]/30">
                        <td className="px-3 py-3 font-bold text-white sm:px-5 sm:py-4">{appt.name || "—"}</td>
                        <td className="px-3 py-3 font-mono text-xs text-sky-300 sm:px-5 sm:py-4">{appt.email || "—"}</td>
                        <td className="px-3 py-3 font-mono text-xs sm:px-5 sm:py-4">{appt.phone || "—"}</td>
                        <td className="px-3 py-3 sm:px-5 sm:py-4">
                          <span className="rounded border border-indigo-500/20 bg-indigo-500/10 px-2 py-0.5 text-xs font-semibold text-indigo-300">
                            {appt.date || "—"}
                          </span>
                        </td>
                        <td className="px-3 py-3 sm:px-5 sm:py-4">
                          <span className="rounded border border-sky-500/20 bg-sky-500/10 px-2 py-0.5 text-xs font-semibold text-sky-300">
                            {appt.time || "—"}
                          </span>
                        </td>
                        <td className="px-3 py-3 sm:px-5 sm:py-4">
                          <span className={`rounded border px-2 py-0.5 text-xs font-bold ${appt.status === "confirmed"
                              ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-400"
                              : appt.status === "cancelled"
                                ? "border-rose-500/20 bg-rose-500/10 text-rose-400"
                                : "border-slate-500/20 bg-slate-500/10 text-slate-400"
                            }`}>
                            {appt.status || "pending"}
                          </span>
                        </td>
                        <td className="max-w-[120px] truncate px-3 py-3 text-xs text-slate-400 sm:max-w-[180px] sm:px-5 sm:py-4">{appt.notes || "—"}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ── Orders Panel ── */}
        {activeTab === "orders" && (
          <div className="flex h-full flex-col gap-4 p-4 sm:gap-5 sm:p-6">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className="text-lg font-bold tracking-tight text-white">🛒 Customer Orders</h2>
                <p className="mt-0.5 text-xs text-slate-500">Placed via voice or chat — pending agent follow-up</p>
              </div>
              <button
                onClick={async () => {
                  try {
                    const res = await fetch(`${backendUrl}/api/orders`, {
                      headers: getHeaders()
                    });
                    if (res.ok) {
                      const data = await res.json();
                      setOrders(data.orders || []);
                    }
                  } catch { }
                }}
                className="rounded-lg border border-sky-500/20 bg-sky-500/10 px-3 py-1.5 text-xs font-bold text-sky-400 transition-all hover:bg-sky-500/20"
              >
                ↻ Refresh
              </button>
            </div>
            <div className="flex-1 overflow-auto rounded-xl border border-[#1E293B]">
              <table className="w-full min-w-[760px] text-sm text-slate-300">
                <thead>
                  <tr className="border-b border-[#1E293B] bg-[#0F172A] text-xs uppercase tracking-widest text-slate-500">
                    <th className="px-3 py-3 text-left sm:px-5">Order #</th>
                    <th className="px-3 py-3 text-left sm:px-5">Customer</th>
                    <th className="px-3 py-3 text-left sm:px-5">Email</th>
                    <th className="px-3 py-3 text-left sm:px-5">Phone</th>
                    <th className="px-3 py-3 text-left sm:px-5">Product</th>
                    <th className="px-3 py-3 text-left sm:px-5">Total</th>
                    <th className="px-3 py-3 text-left sm:px-5">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1E293B]/60">
                  {orders.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="py-16 text-center text-xs font-semibold text-slate-500">
                        <div className="flex flex-col items-center gap-2 px-4">
                          <span className="text-3xl">📦</span>
                          <span>No orders yet. Say &quot;I&apos;ll take the professional package&quot; on a Vapi call to place one!</span>
                        </div>
                      </td>
                    </tr>
                  ) : (
                    orders.map((order, idx) => (
                      <tr key={idx} className="transition-all hover:bg-[#1E293B]/30">
                        <td className="px-3 py-3 font-mono text-xs font-bold text-white sm:px-5 sm:py-4">#{order.order_id || "—"}</td>
                        <td className="px-3 py-3 font-bold text-white sm:px-5 sm:py-4">{order.customer_name || "—"}</td>
                        <td className="px-3 py-3 font-mono text-xs text-sky-300 sm:px-5 sm:py-4">{order.customer_email || "—"}</td>
                        <td className="px-3 py-3 font-mono text-xs sm:px-5 sm:py-4">{order.customer_phone || "—"}</td>
                        <td className="px-3 py-3 text-xs sm:px-5 sm:py-4">{order.product_name || "—"}</td>
                        <td className="px-3 py-3 sm:px-5 sm:py-4">
                          <span className="rounded border border-indigo-500/20 bg-indigo-500/10 px-2 py-0.5 text-xs font-semibold text-indigo-300">
                            {order.total_price || "—"}
                          </span>
                        </td>
                        <td className="px-3 py-3 sm:px-5 sm:py-4">
                          <span className={`rounded border px-2 py-0.5 text-xs font-bold ${order.status === "pending"
                              ? "border-amber-500/20 bg-amber-500/10 text-amber-400"
                              : order.status === "completed"
                                ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-400"
                                : order.status === "cancelled"
                                  ? "border-rose-500/20 bg-rose-500/10 text-rose-400"
                                  : "border-slate-500/20 bg-slate-500/10 text-slate-400"
                            }`}>
                            {order.status || "pending"}
                          </span>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {activeTab === "admin" && (
          <AdminIntegrations backendUrl={backendUrl} getHeaders={getHeaders} />
        )}
      </main>
    </div>
  );
}
