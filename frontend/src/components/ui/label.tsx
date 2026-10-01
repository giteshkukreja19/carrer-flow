import type { LabelHTMLAttributes } from "react";
import { cn } from "@/lib/utils";
export function Label({ className, ...p }: LabelHTMLAttributes<HTMLLabelElement>) { return <label className={cn("block", className)} {...p} />; }
