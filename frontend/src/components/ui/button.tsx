import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";
export function Button({ className, variant, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: string }) { return <button className={cn("inline-flex items-center justify-center rounded-md px-3 py-2 text-sm transition disabled:cursor-not-allowed disabled:opacity-50", variant === "outline" ? "border bg-transparent" : "bg-white text-black", className)} {...props} />; }
