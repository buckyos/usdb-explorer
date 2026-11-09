# Exercise the pinned backend parser with an in-memory RPC transport, never a live node.
Logger.configure(level: :error)
{:ok, _} = Application.ensure_all_started(:ethereum_jsonrpc)
settings = Application.fetch_env!(:ethereum_jsonrpc, EthereumJSONRPC.Geth)
if settings[:block_traceable?], do: raise("USDB must use transaction-hash tracing")
if settings[:tracer] != "call_tracer", do: raise("USDB must use the native callTracer")

defmodule ExplorerTraceFixtureTransport do
  def json_rpc(requests, _options) do
    responses = Enum.map(requests, fn request ->
      if request.method != "debug_traceTransaction", do: raise("Unexpected RPC method")
      [hash, options] = request.params
      if options["tracer"] != "callTracer", do: raise("Unexpected tracer")
      Process.put(:trace_requests, [hash | Process.get(:trace_requests, [])])
      %{id: request.id, result: Map.fetch!(Process.get(:trace_results), hash)}
    end)
    # Real JSON-RPC batch responses need not preserve request order.
    {:ok, Enum.reverse(responses)}
  end
end

input = IO.read(:stdio, :eof) |> Jason.decode!()
transactions = Enum.map(input["transactions"], fn transaction ->
  %{block_number: transaction["block_number"], hash_data: transaction["hash"],
    transaction_index: transaction["index"]}
end)
Process.put(:trace_results, input["traces"])

# Reproduce the original failure in the exact published Blockscout parser.
if transactions != [] do
  first = hd(transactions)
  wrapper = %{"result" => Map.fetch!(input["traces"], first.hash_data)}
  try do
    EthereumJSONRPC.Geth.to_transactions_params([%{id: 0, result: [wrapper]}], %{0 => first.block_number})
    raise("Block tracing unexpectedly accepted a result without txHash")
  rescue
    FunctionClauseError -> :ok
  end
end

arguments = [transport: ExplorerTraceFixtureTransport,
             transport_options: [url: "http://fixture.invalid", http_options: []]]
{:ok, decoded} = EthereumJSONRPC.Geth.fetch_internal_transactions(transactions, arguments)
IO.puts("USDB_TRACE_RESULT=" <> Jason.encode!(%{decoded: decoded, requests: Process.get(:trace_requests, [])}))
