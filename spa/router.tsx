import {
  Outlet,
  RouterProvider,
  createHashHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AppErrorComponent } from "@/lib/error-component";
import { Home } from "@/routes/index";
import { NewEngagement } from "@/routes/new";
import { EngagementPage } from "@/routes/e.$id.index";
import { ReportPage } from "@/routes/e.$id.report";

const rootRoute = createRootRoute({
  component: () => (
    <TooltipProvider delayDuration={200}>
      <Outlet />
    </TooltipProvider>
  ),
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: Home,
});

const newRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/new",
  component: NewEngagement,
});

const engagementRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/e/$id",
  component: () => <Outlet />,
});

const engagementIndexRoute = createRoute({
  getParentRoute: () => engagementRoute,
  path: "/",
  component: EngagementPage,
});

const reportRoute = createRoute({
  getParentRoute: () => engagementRoute,
  path: "report",
  component: ReportPage,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  newRoute,
  engagementRoute.addChildren([engagementIndexRoute, reportRoute]),
]);

const router = createRouter({
  routeTree,
  history: createHashHistory(),
  defaultErrorComponent: AppErrorComponent,
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

export function AppRouter() {
  return <RouterProvider router={router} />;
}
