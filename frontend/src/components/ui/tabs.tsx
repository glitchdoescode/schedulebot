// frontend/src/components/ui/tabs.tsx
"use client"

import * as React from "react"
import * as TabsPrimitive from "@radix-ui/react-tabs"
import { cn } from "@/lib/utils"

const Tabs = TabsPrimitive.Root

interface TabsListProps
  extends React.ComponentPropsWithoutRef<typeof TabsPrimitive.List> {
  className?: string
}

const TabsList = React.forwardRef(
  ({ className, ...props }: TabsListProps, ref: React.ForwardedRef<HTMLDivElement>) => {
    return (
      <TabsPrimitive.List
        ref={ref}
        className={cn(
          "inline-flex items-center justify-center rounded-md bg-muted p-1 text-muted-foreground",
          className
        )}
        {...props}
      />
    )
  }
)
TabsList.displayName = "TabsList"

interface TabsTriggerProps
  extends React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger> {
  className?: string
}

const TabsTrigger = React.forwardRef(
  ({ className, ...props }: TabsTriggerProps, ref: React.ForwardedRef<HTMLButtonElement>) => {
    return (
      <TabsPrimitive.Trigger
        ref={ref}
        className={cn(
          "inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium ring-offset-background transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm",
          className
        )}
        {...props}
      />
    )
  }
)
TabsTrigger.displayName = "TabsTrigger"

interface TabsContentProps
  extends React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content> {
  className?: string
}

const TabsContent = React.forwardRef(
  ({ className, ...props }: TabsContentProps, ref: React.ForwardedRef<HTMLDivElement>) => {
    return (
      <TabsPrimitive.Content
        ref={ref}
        className={cn(
          "mt-2 ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
          className
        )}
        {...props}
      />
    )
  }
)
TabsContent.displayName = "TabsContent"

export { Tabs, TabsList, TabsTrigger, TabsContent }