import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Dashboard from "@/pages/Dashboard";
import LoginPage from "@/pages/LoginPage";
import { apiGet } from "@/lib/api";

export default function App() {
  const queryClient = useQueryClient();
  const [sessionExpired, setSessionExpired] = useState(false);
  const session = useQuery({
    queryKey: ["auth", "me"],
    queryFn: () => apiGet<{ authenticated: boolean; username: string }>("/auth/me"),
    retry: false,
  });
  useEffect(() => {
    const expire = () => setSessionExpired(true);
    window.addEventListener("career-flow-session-expired", expire);
    return () => window.removeEventListener("career-flow-session-expired", expire);
  }, []);

  if (session.isLoading) {
    return <div className="flex min-h-svh items-center justify-center bg-[#090d16] font-mono text-xs uppercase tracking-widest text-slate-500">Loading Career Flow…</div>;
  }
  if (session.isError || sessionExpired || !session.data) {
    return <LoginPage onLoggedIn={(identity) => { setSessionExpired(false); queryClient.setQueryData(["auth", "me"], identity); }} />;
  }
  return <Dashboard onLoggedOut={() => { setSessionExpired(true); queryClient.setQueryData(["auth", "me"], undefined); }} />;
}
