import './App.css'
import { Card } from './components/ui/card'
import SheetManager from './components/ui/sheetmanager'
import PromptInput from './components/ui/promptinput'
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import { Table2, MessageSquareText } from "lucide-react"

function App() {
  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-50 to-slate-100">
      {/* Header */}
      <header className="sticky top-0 z-50 border-b border-slate-200 bg-white/80 backdrop-blur-sm">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
              <Table2 className="h-5 w-5 text-primary-foreground" />
            </div>
            <span className="text-lg font-bold tracking-tight">Excel Analyst</span>
          </div>
        </div>
      </header>

      {/* Main content */}
      <main className="mx-auto max-w-5xl px-6 py-10">
        {/* Hero */}
        <div className="mb-10 text-center">
          <h1 className="text-4xl font-extrabold tracking-tight text-slate-900 lg:text-5xl">
            Query your sheets in minutes
          </h1>
          <p className="mt-3 text-base text-muted-foreground">
            Upload an Excel file, describe your sheets, and ask questions — the AI handles the rest.
          </p>
        </div>

        {/* Tabs */}
        <Tabs defaultValue="upload">
          <TabsList className="grid w-full grid-cols-2 rounded-xl">
            <TabsTrigger value="upload" className="flex items-center gap-2 rounded-xl py-2.5">
              <Table2 className="h-4 w-4" />
              Upload &amp; Describe
            </TabsTrigger>
            <TabsTrigger value="query" className="flex items-center gap-2 rounded-xl py-2.5">
              <MessageSquareText className="h-4 w-4" />
              Query
            </TabsTrigger>
          </TabsList>

          <TabsContent value="upload">
            <Card className="p-6 sm:p-8">
              <div className="mb-6">
                <h2 className="text-xl font-semibold tracking-tight">
                  Upload your Excel file and describe your sheets
                </h2>
                <p className="mt-1.5 text-sm text-muted-foreground">
                  Upload an Excel file with multiple sheets. After upload, describe each
                  sheet so the AI can better understand your data and perform cross-sheet
                  calculations.
                </p>
              </div>
              <SheetManager />
            </Card>
          </TabsContent>

          <TabsContent value="query">
            <Card className="p-6 sm:p-8">
              <div className="mb-6">
                <h2 className="text-xl font-semibold tracking-tight">
                  Have a question about your document?
                </h2>
                <p className="mt-1.5 text-sm text-muted-foreground">
                  Ask anything about your uploaded data. The AI will analyze your sheets and provide answers with calculations.
                </p>
              </div>
              <PromptInput />
            </Card>
          </TabsContent>
        </Tabs>
      </main>
    </div>
  )
}

export default App
