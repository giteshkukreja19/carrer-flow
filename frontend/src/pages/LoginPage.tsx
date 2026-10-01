import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { LockKeyhole, Radio } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, apiPost } from "@/lib/api";

interface Identity { authenticated: boolean; username: string }

export default function LoginPage({ onLoggedIn }: { onLoggedIn: (identity: Identity) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => apiPost<Identity>("/auth/login", { username, password }),
    onSuccess: (identity) => {
      setPassword("");
      onLoggedIn(identity);
    },
  });
  const message = login.error instanceof ApiError && login.error.status === 401
    ? "Username or password is incorrect."
    : login.isError
      ? "Login is unavailable. Check server configuration and try again."
      : "";

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    login.mutate();
  }

  return <main className="flex min-h-svh items-center justify-center bg-[#090d16] px-4 py-10 text-slate-200">
    <div className="w-full max-w-md">
      <div className="mb-6 flex items-center justify-center gap-3">
        <div className="flex size-10 items-center justify-center rounded-lg border border-indigo-400/30 bg-indigo-500/10 text-indigo-300"><Radio className="size-5" /></div>
        <div><div className="font-heading text-sm font-semibold tracking-[0.18em] text-white">CAREER FLOW</div><div className="font-mono text-[9px] uppercase tracking-[0.18em] text-slate-500">PRIVATE PLACEMENT DASHBOARD</div></div>
      </div>
      <Card className="border-slate-800 bg-[#111726]/90 shadow-2xl shadow-black/30">
        <CardHeader><CardTitle className="flex items-center gap-2 font-heading text-xl text-white"><LockKeyhole className="size-4 text-indigo-300" /> Sign in</CardTitle><p className="text-sm text-slate-400">Use the account configured by the server administrator.</p></CardHeader>
        <CardContent>
          <form onSubmit={submit} className="space-y-5">
            <div><Label htmlFor="login-username" className="font-mono text-[10px] uppercase tracking-wider text-slate-500">Username</Label><Input id="login-username" name="username" autoComplete="username" required value={username} onChange={(event) => setUsername(event.target.value)} className="mt-2 border-slate-700 bg-slate-950/60 text-white" /></div>
            <div><Label htmlFor="login-password" className="font-mono text-[10px] uppercase tracking-wider text-slate-500">Password</Label><Input id="login-password" name="password" type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} className="mt-2 border-slate-700 bg-slate-950/60 text-white" /></div>
            {message && <p role="alert" className="text-sm text-rose-300">{message}</p>}
            <Button type="submit" disabled={login.isPending} className="w-full bg-slate-100 text-slate-950 hover:bg-white">{login.isPending ? "Signing in…" : "Sign in"}</Button>
          </form>
          <p className="mt-5 border-t border-slate-800 pt-4 text-xs leading-5 text-slate-500">Credentials stay on the server. The session uses an HttpOnly cookie; passwords are not saved in browser storage.</p>
        </CardContent>
      </Card>
    </div>
  </main>;
}
