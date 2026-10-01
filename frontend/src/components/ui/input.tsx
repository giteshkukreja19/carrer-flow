import type { InputHTMLAttributes } from "react";
import { cn } from "@/lib/utils";
export function Input({ className, ...p }: InputHTMLAttributes<HTMLInputElement>) { return <input className={cn("w-full rounded-md border px-3 py-2 outline-none", className)} {...p} />; }
