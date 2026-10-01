import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";
export function Card({ className, ...p }: HTMLAttributes<HTMLDivElement>) { return <div className={cn("rounded-xl border", className)} {...p} />; }
export function CardHeader({ className, ...p }: HTMLAttributes<HTMLDivElement>) { return <div className={cn("p-5 pb-3", className)} {...p} />; }
export function CardContent({ className, ...p }: HTMLAttributes<HTMLDivElement>) { return <div className={cn("p-5", className)} {...p} />; }
export function CardTitle({ className, ...p }: HTMLAttributes<HTMLHeadingElement>) { return <h3 className={cn("text-lg font-semibold", className)} {...p} />; }
